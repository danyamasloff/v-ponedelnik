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

#: What to say when we have no usable product name. "обновил обновление" is
#: what a naive fallback produces, so these events get their own wording.
FALLBACK_PHRASES: dict[EventType, str] = {
    EventType.RELEASE: "выпустил обновление",
    EventType.UPDATE: "выпустил обновление",
    EventType.OTHER: "сообщил об обновлении",
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
    # A product extracted as a bare lowercase word ("scheduled", "automation")
    # produces a sentence that reads as broken Russian. Better to name only the
    # vendor and the event than to publish a fragment as if it were a product.
    if product and _looks_like_a_name(product):
        subject = " ".join(part for part in (product, version) if part).strip()
        return f"{label} {phrase} {subject}."
    # Some phrases already carry their object ("опубликовал руководство"); the
    # transitive ones get a fallback object instead of being left dangling.
    return f"{label} {FALLBACK_PHRASES.get(event_type, phrase)}."


def _looks_like_a_name(product: str) -> bool:
    """Whether the extracted product reads as a product name, not a stray word."""
    return any(part[:1].isupper() or any(ch.isdigit() for ch in part) for part in product.split())


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
    analysis: str | None = None,
    headline: str | None = None,
) -> PostDraft:
    """Assemble the post for one platform.

    Without ``analysis`` this is a skeleton and says so. With it — written by
    PHASE 5 generation from the same facts — the placeholder is replaced and
    the draft becomes publishable. The facts, the title and the source link are
    never touched by the generator.
    """
    # A generated Russian headline wins over the source's own title, which is
    # often in another language. The original stays in ``PostDraft.title``.
    shown_title = (headline or title).strip()
    lines: list[str] = [f"**{shown_title}**", ""]

    facts = fact_line(vendor=vendor, product=product, version=version, event_type=event_type)
    if facts:
        lines += [facts, ""]

    lines += [analysis.strip() if analysis and analysis.strip() else ANALYSIS_PLACEHOLDER, ""]

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
