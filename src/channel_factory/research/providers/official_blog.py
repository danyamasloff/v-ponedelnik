"""Minimal whitelist reader for official pages that publish no feed.

Deliberately **not** a general web scraper. It does exactly one thing: read a
listing page belonging to a source that is already on the whitelist, and take
the links that live under that section together with their anchor text.

It does not follow links, does not fetch article bodies, does not execute
JavaScript and has no per-site extraction rules. If a specific important source
ever needs more than this, it gets its own explicit adapter — the answer is
never to make this file cleverer.
"""

from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

from channel_factory.core.enums import ResearchProviderType
from channel_factory.research.providers.base import (
    DEFAULT_TIMEOUT_SECONDS,
    USER_AGENT,
    ProviderError,
    RawResearchItem,
    SourceConfig,
)

MIN_TITLE_LENGTH = 12
MAX_LINKS = 60


class _LinkCollector(HTMLParser):
    """Collects ``(href, anchor text)`` pairs."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href")
        if href:
            self._href = href
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "a" and self._href is not None:
            text = " ".join("".join(self._text).split())
            self.links.append((self._href, text))
            self._href = None
            self._text = []


class OfficialBlogProvider:
    """Reads a listing page of a whitelisted official source."""

    provider_type = ResearchProviderType.OFFICIAL_BLOG

    async def fetch(self, source: SourceConfig) -> list[RawResearchItem]:
        if not source.url:
            raise ProviderError(f"source {source.key!r} has no url")

        async with httpx.AsyncClient(
            timeout=DEFAULT_TIMEOUT_SECONDS, follow_redirects=True
        ) as client:
            try:
                response = await client.get(source.url, headers={"User-Agent": USER_AGENT})
            except httpx.HTTPError as exc:
                raise ProviderError(f"request failed for {source.url}: {exc}") from exc

        if response.status_code >= 400:
            raise ProviderError(f"{source.url} returned HTTP {response.status_code}")

        collector = _LinkCollector()
        collector.feed(response.text)

        base = urlsplit(source.url)
        # Only links under the same host and the same section count: that is
        # what keeps this a whitelist reader rather than a crawler.
        section = (source.config or {}).get("section") or base.path.rstrip("/")
        seen: set[str] = set()
        items: list[RawResearchItem] = []

        for href, text in collector.links:
            absolute = urljoin(source.url, href)
            parts = urlsplit(absolute)
            if parts.netloc != base.netloc:
                continue
            if section and not parts.path.startswith(section):
                continue
            if parts.path.rstrip("/") == section:
                continue
            if len(text) < MIN_TITLE_LENGTH:
                continue
            clean = absolute.split("#", 1)[0]
            if clean in seen:
                continue
            seen.add(clean)
            items.append(
                RawResearchItem(
                    # The page gives no id of its own, so the URL is the
                    # documented fallback identity.
                    external_id=clean,
                    url=clean,
                    title=text[:1024],
                    summary=None,
                    content_excerpt=None,
                    # Listing pages rarely carry a machine-readable date; the
                    # engine falls back to discovery time and says so, rather
                    # than inventing a publication date.
                    published_at=None,
                    publisher=base.netloc,
                    raw_metadata={"listing_url": source.url},
                )
            )
            if len(items) >= MAX_LINKS:
                break

        if not items:
            raise ProviderError(
                f"no links matched section {section!r} at {source.url}; "
                "the page layout may have changed"
            )
        return items
