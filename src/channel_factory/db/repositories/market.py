"""Repositories for niches, market channels and their snapshots."""

from __future__ import annotations

from typing import Any

from sqlalchemy import insert, select
from sqlalchemy.ext.asyncio import AsyncSession

from channel_factory.core.enums import Platform
from channel_factory.db.models import MarketChannel, MarketSnapshot, Niche
from channel_factory.direct.normalization.rules import slugify

# Chunk size for IN (...) lookups, so a large import never builds one huge query.
LOOKUP_CHUNK_SIZE = 1000


def _chunks(items: list[Any], size: int) -> list[list[Any]]:
    return [items[start : start + size] for start in range(0, len(items), size)]


class NicheRepository:
    """Niche lookup and on-demand creation from category labels."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create_many(self, labels: set[str]) -> dict[str, Niche]:
        """Map raw category labels to :class:`Niche` rows, creating what is missing.

        The returned dict is keyed by the original label so callers do not need
        to know about slugging.
        """
        if not labels:
            return {}

        by_slug = {slugify(label): label for label in labels}
        existing: dict[str, Niche] = {}
        for chunk in _chunks(list(by_slug), LOOKUP_CHUNK_SIZE):
            result = await self._session.execute(select(Niche).where(Niche.slug.in_(chunk)))
            for niche in result.scalars():
                existing[niche.slug] = niche

        created: list[Niche] = []
        for slug, label in by_slug.items():
            if slug not in existing:
                niche = Niche(slug=slug, name=label, raw_label=label)
                created.append(niche)
                existing[slug] = niche
        if created:
            self._session.add_all(created)
            await self._session.flush()

        return {label: existing[slug] for slug, label in by_slug.items()}


class MarketChannelRepository:
    """Identity resolution for channels seen in market data."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_by_dedupe_keys(
        self, keys: set[str]
    ) -> dict[tuple[Platform, str], MarketChannel]:
        """Load existing channels keyed by ``(platform, dedupe_key)``."""
        if not keys:
            return {}
        found: dict[tuple[Platform, str], MarketChannel] = {}
        for chunk in _chunks(list(keys), LOOKUP_CHUNK_SIZE):
            result = await self._session.execute(
                select(MarketChannel).where(MarketChannel.dedupe_key.in_(chunk))
            )
            for channel in result.scalars():
                found[(channel.platform, channel.dedupe_key)] = channel
        return found

    def add_all(self, channels: list[MarketChannel]) -> None:
        self._session.add_all(channels)


class MarketSnapshotRepository:
    """Append-only metric history."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def insert_many(
        self, snapshots: list[dict[str, Any]], *, chunk_size: int = 1000
    ) -> None:
        """Batch-insert snapshots."""
        if not snapshots:
            return
        for chunk in _chunks(snapshots, chunk_size):
            await self._session.execute(insert(MarketSnapshot), chunk)
