"""Turning a research cluster into a post skeleton.

This is **not** the Content Engine. It assembles only what we can state as
fact from our own extracted fields — who released what, of which version, and
where to read the primary source — and leaves an explicit placeholder where the
original analysis belongs.

That boundary is the point. Copying a source's own summary into a post would be
exactly the plagiarism the project forbids, so the draft carries facts and a
link, and refuses to call itself publishable until a human or the PHASE 5
generator has written the part that gives the reader something to do.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from channel_factory.core.enums import EventType, Platform

ANALYSIS_PLACEHOLDER = "[TODO PHASE 5: оригинальный разбор — что читатель сможет сделать]"

EVENT_PHRASES: dict[EventType, str] = {
    EventType.RELEASE: "выпустил",
    EventType.UPDATE: "обновил",
    EventType.NEW_TOOL: "представил инструмент",
    EventType.RESEARCH: "опубликовал исследование",
    EventType.GUIDE: "опубликовал руководство",
    EventType.INCIDENT: "сообщил об инциденте",
    EventType.OTHER: "сообщил",
}

VENDOR_NAMES: dict[str, str] = {
    "openai": "OpenAI",
    "anthropic": "Anthropic",
    "google": "Google",
    "microsoft": "Microsoft",
    "github": "GitHub",
    "cursor": "Cursor",
    "notion": "Notion",
    "zapier": "Zapier",
    "ollama": "Ollama",
    "huggingface": "Hugging Face",
    "n8n": "n8n",
    "meta": "Meta",
    "mistral": "Mistral",
    "perplexity": "Perplexity",
    "figma": "Figma",
    "canva": "Canva",
}


@dataclass
class PostDraft:
    """A post skeleton with its unfinished parts named."""

    platform: Platform
    text: str
    title: str
    cluster_id: str
    sources: list[str] = field(default_factory=list)
    placeholders: list[str] = field(default_factory=list)

    @property
    def is_publishable(self) -> bool:
        """A skeleton is never publishable; that is what makes it a skeleton."""
        return not self.placeholders

    @property
    def length(self) -> int:
        return len(self.text)


def _vendor_label(vendor: str | None) -> str | None:
    if not vendor:
        return None
    return VENDOR_NAMES.get(vendor, vendor.capitalize())


def fact_line(
    *,
    vendor: str | None,
    product: str | None,
    version: str | None,
    event_type: EventType,
) -> str | None:
    """One sentence of our own, built from fields we extracted ourselves."""
    label = _vendor_label(vendor)
    if not label:
        return None
    phrase = EVENT_PHRASES.get(event_type, EVENT_PHRASES[EventType.OTHER])
    subject = " ".join(part for part in (product, version) if part).strip()
    if subject:
        return f"{label} {phrase} {subject}."
    return f"{label} {phrase} обновление."


def render_draft(
    *,
    platform: Platform,
    cluster_id: str,
    title: str,
    vendor: str | None,
    product: str | None,
    version: str | None,
    event_type: EventType,
    primary_url: str | None,
    primary_source: str | None,
    trust: str | None = None,
    text_limit: int = 4000,
) -> PostDraft:
    """Assemble the skeleton for one platform."""
    lines: list[str] = [f"**{title.strip()}**", ""]

    facts = fact_line(vendor=vendor, product=product, version=version, event_type=event_type)
    if facts:
        lines += [facts, ""]

    lines += [ANALYSIS_PLACEHOLDER, ""]

    sources: list[str] = []
    if primary_url:
        label = primary_source or "источник"
        suffix = f" ({trust})" if trust else ""
        lines.append(f"Источник: {label}{suffix} — {primary_url}")
        sources.append(primary_url)

    text = "\n".join(lines).strip()
    if len(text) > text_limit:
        text = text[: text_limit - 1].rstrip() + "…"

    return PostDraft(
        platform=platform,
        text=text,
        title=title,
        cluster_id=cluster_id,
        sources=sources,
        placeholders=[ANALYSIS_PLACEHOLDER] if ANALYSIS_PLACEHOLDER in text else [],
    )
