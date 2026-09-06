"""Provider contract.

A provider knows how to read one kind of source and nothing else: it does not
touch the database, does not decide relevance, and does not deduplicate. It
returns raw items; everything downstream is the engine's job.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from channel_factory.core.enums import ResearchProviderType, TrustLevel

USER_AGENT = "yan-channel-factory/0.1 (research engine; contact via repository)"
DEFAULT_TIMEOUT_SECONDS = 20.0
EXCERPT_LIMIT = 1200


class ProviderError(Exception):
    """A source could not be read. Never fatal to a whole poll run."""


@dataclass(frozen=True)
class SourceConfig:
    """One entry of the source whitelist."""

    key: str
    name: str
    provider: ResearchProviderType
    trust: TrustLevel
    url: str | None = None
    repo: str | None = None
    enabled: bool = True
    poll_interval_minutes: int = 60
    reason: str | None = None
    # Whether the channel's audience can open this source. MAX is a Russian
    # network: a link its readers cannot follow is worse than no link, so a
    # source marked VPN is credited by name and its URL is left out of posts.
    audience_access: str = "VPN"
    config: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RawResearchItem:
    """One document as the source presented it, before any interpretation."""

    external_id: str
    url: str
    title: str
    summary: str | None = None
    content_excerpt: str | None = None
    published_at: datetime | None = None
    author: str | None = None
    publisher: str | None = None
    language: str | None = None
    raw_metadata: dict[str, Any] = field(default_factory=dict)


class ResearchProvider(Protocol):
    """Reads one source and returns its current items."""

    provider_type: ResearchProviderType

    async def fetch(self, source: SourceConfig) -> list[RawResearchItem]:
        """Return items currently visible at the source."""
        ...
