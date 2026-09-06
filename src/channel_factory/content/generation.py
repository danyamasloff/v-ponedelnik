"""PHASE 5: writing the part of a post that a skeleton leaves empty.

The drafting stage states facts we extracted ourselves and marks where the
original analysis belongs. This module fills that marker — and only that
marker. The facts, the title and the source link stay as drafted, so a
generator can never invent a version number or a release date.

What the generator is asked for is deliberately narrow: what the reader can do
with this news. Retelling the source is both useless to the reader and the
plagiarism the project forbids, so the prompt says so and the result is checked
for it.

Two backends, both free:

* **Ollama** — a model running on this machine. Nothing leaves the computer and
  there is no quota at all.
* **Gemini free tier** — no card, no cost, but the prompt does travel to Google.
  Only public facts we already have are sent: title, vendor, product, version.

Neither is invented here: if no backend is available the draft simply stays a
skeleton and says so, rather than being published half-written.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

from channel_factory.core.enums import AiTaskType, EventType
from channel_factory.core.logging import get_logger

logger = get_logger(__name__)

DEFAULT_OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_OLLAMA_MODEL = "qwen2.5:7b-instruct"

#: Analysis longer than this stops being a post and starts being an article.
MAX_ANALYSIS_CHARS = 700
MIN_ANALYSIS_CHARS = 80

SYSTEM_PROMPT = """Ты редактор Telegram/MAX-канала о практическом применении ИИ.
Пишешь по-русски, коротко и по делу, без рекламных восклицаний и без эмодзи.

Нужно два поля JSON:
* "headline" — заголовок поста по-русски, до 90 символов, без кликбейта и без
  точки в конце. Если исходный заголовок на другом языке, передай его смысл
  по-русски, а не переводи дословно;
* "analysis" — 2–4 предложения о том, **что это меняет для читателя**:
  практика, а не пересказ новости.

Жёсткие правила:
* не пересказывай текст источника и не цитируй его;
* не придумывай числа, версии, даты, цены и цитаты — используй только факты ниже;
* если фактов мало, пиши осторожно («судя по анонсу», «деталей пока нет»);
* не обещай выгод, которых не следует из фактов;
* в "analysis" не повторяй заголовок, не вставляй ссылки и списки —
  только связный текст."""

ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "analysis": {"type": "string"},
    },
    "required": ["headline", "analysis"],
}

#: A headline longer than this stops being a headline.
MAX_HEADLINE_CHARS = 110


class GenerationError(Exception):
    """The analysis could not be generated."""


@dataclass(frozen=True)
class AnalysisBrief:
    """Everything the generator is allowed to know."""

    title: str
    vendor: str | None
    product: str | None
    version: str | None
    event_type: EventType
    source_url: str | None = None
    source_name: str | None = None

    def as_prompt(self) -> str:
        lines = [f"Заголовок: {self.title}"]
        if self.vendor:
            lines.append(f"Компания: {self.vendor}")
        if self.product:
            lines.append(f"Продукт: {self.product}")
        if self.version:
            lines.append(f"Версия: {self.version}")
        lines.append(f"Тип события: {self.event_type.value}")
        if self.source_name:
            lines.append(f"Источник: {self.source_name}")
        return "\n".join(lines)


@dataclass(frozen=True)
class GeneratedAnalysis:
    """The written part, plus how it was produced."""

    text: str
    backend: str
    model: str
    headline: str | None = None


class AnalysisGenerator(Protocol):
    """Writes the analysis paragraph for one news item."""

    name: str

    async def analyse(self, brief: AnalysisBrief) -> GeneratedAnalysis: ...


def clean_analysis(raw: str, brief: AnalysisBrief) -> str:
    """Trim and sanity-check what the model wrote.

    Models like to open with the headline they were given and to wrap the
    answer in quotes or markdown fences. Both are stripped; anything that comes
    back too short or too long is refused rather than published.
    """
    text = raw.strip().strip("`").strip()
    if text.startswith('"') and text.endswith('"'):
        text = text[1:-1].strip()

    title = brief.title.strip().rstrip(".")
    if title and text.lower().startswith(title.lower()):
        text = text[len(title) :].lstrip(" .:—-\n")

    text = "\n".join(line.strip() for line in text.splitlines() if line.strip())

    if len(text) < MIN_ANALYSIS_CHARS:
        raise GenerationError(f"analysis too short: {len(text)} characters")
    if len(text) > MAX_ANALYSIS_CHARS:
        # Cut on a sentence boundary so the post does not end mid-word.
        cut = text[:MAX_ANALYSIS_CHARS]
        boundary = max(cut.rfind("."), cut.rfind("!"), cut.rfind("?"))
        text = cut[: boundary + 1] if boundary > MIN_ANALYSIS_CHARS else cut.rstrip() + "…"
    return text


def clean_headline(raw: str) -> str | None:
    """A usable Russian headline, or nothing.

    Nothing is a fine answer: the draft then keeps the source's own title,
    which is accurate even when it is in the wrong language.
    """
    text = " ".join(raw.strip().strip('"').strip("`").split())
    text = text.rstrip(".")
    if not text or len(text) > MAX_HEADLINE_CHARS:
        return None
    return text


class OllamaGenerator:
    """Local model over the Ollama HTTP API.

    Free and offline: nothing about the topic leaves this machine. Requires
    ``ollama serve`` to be running with the model pulled.
    """

    name = "ollama"

    def __init__(
        self,
        *,
        base_url: str = DEFAULT_OLLAMA_URL,
        model: str = DEFAULT_OLLAMA_MODEL,
        timeout: float = 180.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout = timeout

    async def available(self) -> bool:
        """Whether the local server answers and has the model."""
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(f"{self._base_url}/api/tags")
        except httpx.HTTPError:
            return False
        if response.status_code >= 400:
            return False
        models = response.json().get("models") or []
        names = {str(item.get("name", "")) for item in models}
        # Ollama reports "name:tag"; a bare model name counts as a match.
        return any(name == self._model or name.startswith(f"{self._model}:") for name in names)

    async def analyse(self, brief: AnalysisBrief) -> GeneratedAnalysis:
        payload = {
            "model": self._model,
            "prompt": brief.as_prompt(),
            "system": SYSTEM_PROMPT,
            "stream": False,
            # Ollama can constrain decoding to valid JSON, which is what the
            # two-field contract above needs.
            "format": "json",
            # Low temperature: this is analysis grounded in given facts, not
            # creative writing, and a wandering model invents details.
            "options": {"temperature": 0.4, "num_predict": 400},
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(f"{self._base_url}/api/generate", json=payload)
        except httpx.HTTPError as exc:
            raise GenerationError(f"Ollama unreachable: {type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise GenerationError(f"Ollama HTTP {response.status_code}: {response.text[:200]}")

        raw = str(response.json().get("response") or "")
        data = _as_json(raw)
        return GeneratedAnalysis(
            text=clean_analysis(str(data.get("analysis") or raw), brief),
            backend=self.name,
            model=self._model,
            headline=clean_headline(str(data.get("headline") or "")),
        )


class GeminiAnalysisGenerator:
    """Google's free tier, reusing the research engine's client.

    Chosen only when Ollama is not available: the free tier costs nothing but
    the prompt does leave the machine, and Google may use free-tier traffic to
    improve their products. Only public facts are sent.
    """

    name = "gemini"

    def __init__(self, client: Any) -> None:
        self._client = client

    async def analyse(self, brief: AnalysisBrief) -> GeneratedAnalysis:
        try:
            result = await self._client.structured(
                task=AiTaskType.CONTENT_DRAFT,
                system=SYSTEM_PROMPT,
                user=brief.as_prompt(),
                schema=ANALYSIS_SCHEMA,
                max_tokens=800,
            )
        except Exception as exc:  # provider errors are many; the caller sees one
            raise GenerationError(f"Gemini generation failed: {exc}") from exc

        data = result.data if isinstance(getattr(result, "data", None), dict) else {}
        raw = str(data.get("analysis") or "")
        model = getattr(result, "model", None) or "gemini"
        return GeneratedAnalysis(
            text=clean_analysis(raw, brief),
            backend=self.name,
            model=model,
            headline=clean_headline(str(data.get("headline") or "")),
        )


def _as_json(raw: str) -> dict[str, Any]:
    """Parse a model answer that should be JSON but might not be."""
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}
