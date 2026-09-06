"""Posts the channel writes itself, not in reaction to someone's news.

Two reasons this track exists, and both come from the channel rather than from
the code:

* **The audience is in MAX.** A post whose value sits behind a link the reader
  cannot open is not a post. Here there is no link at all — everything the
  reader needs is in the message.
* **News alone does not build a channel.** Anyone can repost a release. What
  makes someone stay is material that answers "what do I do on Monday", and
  that has to be written, not aggregated.

The topic list is curated by hand in ``config/evergreen_topics.yaml``: the
angle and the takeaway are editorial decisions, and a generator inventing its
own subjects is how a channel ends up publishing filler. The model writes the
body for a topic *we chose*, and only that.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from channel_factory.content.generation import (
    MAX_ANALYSIS_CHARS,
    AnalysisGenerator,
    GeneratedAnalysis,
    GenerationError,
    clean_action,
    clean_headline,
)
from channel_factory.core.logging import get_logger

logger = get_logger(__name__)

#: An own post carries more than a news reaction, but it is still a post.
MAX_BODY_CHARS = 1800
MIN_BODY_CHARS = 250

EVERGREEN_PROMPT = """Ты ведёшь Telegram/MAX-канал о практическом применении ИИ
и цифровых навыков в работе и учёбе. Пишешь по-русски, спокойно, без рекламных
восклицаний, без эмодзи и без обращений вроде «друзья».

Напиши самостоятельный пост по заданной теме. Не новость, не пересказ чужой
статьи — материал, который читатель применит сегодня.

Нужны три поля JSON:
* "headline" — заголовок до 90 символов, без точки в конце;
* "body" — тело поста, 900–1600 символов. Структура: одна фраза о том, чья это
  боль, затем 3–5 коротких абзацев по существу. Можно нумерованный список
  шагов. Никаких ссылок и никаких названий платных сервисов как рекламы;
* "action" — один конкретный шаг на сегодня, одно предложение.

Жёсткие правила:
* никаких выдуманных цифр, исследований, цитат и «по данным экспертов»;
* не обещай результатов, которых нельзя гарантировать;
* пиши о том, что работает независимо от конкретного вендора;
* если тема требует фактов, которых у тебя нет, — пиши о методе, а не о цифрах."""

EVERGREEN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "body": {"type": "string"},
        "action": {"type": "string"},
    },
    "required": ["headline", "body"],
}


class EvergreenConfigError(Exception):
    """The topic backlog is missing or malformed."""


@dataclass(frozen=True)
class EvergreenTopic:
    """One topic we decided to write about."""

    key: str
    title: str
    angle: str | None = None
    takeaway: str | None = None
    audience: str | None = None

    def as_prompt(self) -> str:
        lines = [f"Тема: {self.title}"]
        if self.angle:
            lines.append(f"Угол: {self.angle}")
        if self.takeaway:
            lines.append(f"Что читатель должен унести: {self.takeaway}")
        if self.audience:
            lines.append(f"Для кого: {self.audience}")
        return "\n".join(lines)


@dataclass(frozen=True)
class EvergreenPost:
    """A finished own post, ready for the publisher."""

    topic: EvergreenTopic
    headline: str
    body: str
    action: str | None
    backend: str
    model: str

    @property
    def text(self) -> str:
        parts = [f"**{self.headline}**", "", self.body]
        if self.action:
            parts += ["", f"Что сделать: {self.action}"]
        return "\n".join(parts).strip()


@lru_cache(maxsize=4)
def load_topics(path: Path) -> tuple[EvergreenTopic, ...]:
    """Read the curated backlog, refusing anything half-filled."""
    if not path.exists():
        raise EvergreenConfigError(f"evergreen topics config not found: {path}")
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("topics")
    if not isinstance(entries, list) or not entries:
        raise EvergreenConfigError(f"'topics' is missing or empty in {path}")

    topics: list[EvergreenTopic] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise EvergreenConfigError(f"topic entry must be a mapping, got {entry!r}")
        key = str(entry.get("key") or "").strip()
        title = str(entry.get("title") or "").strip()
        if not key or not title:
            raise EvergreenConfigError(f"topic needs both key and title: {entry!r}")
        if key in seen:
            raise EvergreenConfigError(f"duplicate topic key: {key!r}")
        seen.add(key)
        topics.append(
            EvergreenTopic(
                key=key,
                title=title,
                angle=entry.get("angle"),
                takeaway=entry.get("takeaway"),
                audience=entry.get("audience"),
            )
        )
    return tuple(topics)


def clean_body(raw: str) -> str:
    """Trim the body and refuse what is too thin to publish."""
    text = raw.strip().strip("`").strip()
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1].strip()
    text = "\n".join(line.strip() for line in text.splitlines()).strip()

    if len(text) < MIN_BODY_CHARS:
        raise GenerationError(f"body too short: {len(text)} characters")
    if len(text) > MAX_BODY_CHARS:
        cut = text[:MAX_BODY_CHARS]
        boundary = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
        text = cut[: boundary + 1] if boundary > MIN_BODY_CHARS else cut.rstrip() + "…"
    return text


async def write_post(generator: AnalysisGenerator, topic: EvergreenTopic) -> EvergreenPost:
    """Ask the configured backend for the body of one own post.

    Reuses whatever generator the pipeline already chose, so an own post costs
    the same as a news post and runs on the same free backend.
    """
    generated = await _generate(generator, topic)
    return EvergreenPost(
        topic=topic,
        headline=generated.headline or topic.title,
        body=generated.text,
        action=generated.action,
        backend=generated.backend,
        model=generated.model,
    )


async def _generate(generator: AnalysisGenerator, topic: EvergreenTopic) -> GeneratedAnalysis:
    """Run the topic through the generator's own transport.

    The backends differ in how they take a prompt, so the shared protocol is
    not enough here: an own post needs its own rubric and a longer body. Each
    backend exposes the pieces needed to do that, and anything unexpected is a
    GenerationError rather than a silently empty post.
    """
    raw = await _call_backend(generator, topic)
    body = clean_body(str(raw.get("body") or ""))
    if MAX_ANALYSIS_CHARS and len(body) < MIN_BODY_CHARS:  # pragma: no cover - guarded above
        raise GenerationError("empty body")
    return GeneratedAnalysis(
        text=body,
        backend=getattr(generator, "name", "unknown"),
        model=str(raw.get("_model") or "unknown"),
        headline=clean_headline(str(raw.get("headline") or "")),
        action=clean_action(str(raw.get("action") or "")),
    )


async def _call_backend(generator: AnalysisGenerator, topic: EvergreenTopic) -> dict[str, Any]:
    """Send the evergreen rubric through whichever backend is configured."""
    prompt = topic.as_prompt()
    call = getattr(generator, "complete_json", None)
    if call is None:
        raise GenerationError(
            f"backend {getattr(generator, 'name', '?')} cannot write own posts"
        )
    return await call(system=EVERGREEN_PROMPT, user=prompt, schema=EVERGREEN_SCHEMA)
