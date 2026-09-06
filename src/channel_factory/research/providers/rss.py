"""RSS and Atom feeds."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime
from html import unescape
from typing import Any

import feedparser

from channel_factory.core.enums import ResearchProviderType
from channel_factory.research.providers.base import (
    EXCERPT_LIMIT,
    USER_AGENT,
    ProviderError,
    RawResearchItem,
    SourceConfig,
)

_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(value: str | None) -> str | None:
    """Plain text from a feed's HTML summary."""
    if not value:
        return None
    text = _TAG_RE.sub(" ", value)
    text = unescape(text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _published(entry: Any) -> datetime | None:
    for field_name in ("published_parsed", "updated_parsed"):
        parsed = getattr(entry, field_name, None)
        if parsed:
            try:
                return datetime(*parsed[:6], tzinfo=UTC)
            except (TypeError, ValueError):
                continue
    return None


class RssProvider:
    """Reads any source that publishes a valid RSS or Atom feed."""

    provider_type = ResearchProviderType.RSS

    async def fetch(self, source: SourceConfig) -> list[RawResearchItem]:
        if not source.url:
            raise ProviderError(f"source {source.key!r} has no url")
        # feedparser is synchronous and does its own HTTP; run it off the loop.
        parsed = await asyncio.to_thread(
            feedparser.parse, source.url, agent=USER_AGENT
        )

        if getattr(parsed, "bozo", False) and not parsed.entries:
            raise ProviderError(
                f"feed {source.url} did not parse: {getattr(parsed, 'bozo_exception', 'unknown')}"
            )
        status = getattr(parsed, "status", None)
        if status is not None and status >= 400:
            raise ProviderError(f"feed {source.url} returned HTTP {status}")

        publisher = getattr(parsed.feed, "title", None) if hasattr(parsed, "feed") else None

        items: list[RawResearchItem] = []
        for entry in parsed.entries:
            link = getattr(entry, "link", None)
            if not link:
                continue
            title = strip_html(getattr(entry, "title", None)) or link
            summary = strip_html(
                getattr(entry, "summary", None) or getattr(entry, "description", None)
            )
            items.append(
                RawResearchItem(
                    # Feeds usually give a guid; the link is the documented
                    # fallback so the natural key always exists.
                    external_id=str(getattr(entry, "id", None) or link),
                    url=link,
                    title=title[:1024],
                    summary=summary[:2000] if summary else None,
                    content_excerpt=summary[:EXCERPT_LIMIT] if summary else None,
                    published_at=_published(entry),
                    author=getattr(entry, "author", None),
                    publisher=publisher,
                    language=getattr(parsed.feed, "language", None)
                    if hasattr(parsed, "feed")
                    else None,
                    raw_metadata={"tags": [t.get("term") for t in getattr(entry, "tags", [])]},
                )
            )
        return items
