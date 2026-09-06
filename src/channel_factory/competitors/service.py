"""Competitor research: niche dossiers and the watchlist."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import delete, select

from channel_factory.competitors.analysis import (
    ChannelProfile,
    CompetitorRepository,
    LeaderMetric,
    NicheBenchmarks,
)
from channel_factory.core.enums import Platform
from channel_factory.core.logging import get_logger
from channel_factory.db.models import CompetitorWatch, MarketChannel, Niche
from channel_factory.db.session import Database

logger = get_logger(__name__)


@dataclass
class NicheDossier:
    """Everything the market data can say about competing in one niche."""

    benchmarks: NicheBenchmarks
    by_subscribers: list[ChannelProfile] = field(default_factory=list)
    by_engagement: list[ChannelProfile] = field(default_factory=list)
    by_price: list[ChannelProfile] = field(default_factory=list)
    engagement_floor: int | None = None
    watched: list[ChannelProfile] = field(default_factory=list)


class CompetitorService:
    """Assembles competitor views and maintains the watchlist."""

    def __init__(self, database: Database) -> None:
        self._database = database

    async def niche_dossier(
        self,
        slug: str,
        *,
        platform: Platform | None = None,
        limit: int = 10,
    ) -> NicheDossier | None:
        """Benchmarks plus leader boards for one niche."""
        async with self._database.session() as session:
            repo = CompetitorRepository(session)
            benchmarks = await repo.niche_benchmarks(slug, platform=platform)
            if benchmarks is None:
                return None

            # Engagement is ranked only among channels at or above the niche
            # median size: below it, a high ERR usually says more about a small
            # denominator than about a strong channel.
            floor = (
                int(benchmarks.subscribers_p50) if benchmarks.subscribers_p50 is not None else None
            )

            dossier = NicheDossier(
                benchmarks=benchmarks,
                engagement_floor=floor,
                by_subscribers=await repo.niche_leaders(
                    slug, platform=platform, metric=LeaderMetric.SUBSCRIBERS, limit=limit
                ),
                by_engagement=await repo.niche_leaders(
                    slug,
                    platform=platform,
                    metric=LeaderMetric.ENGAGEMENT,
                    limit=limit,
                    min_subscribers=floor,
                ),
                by_price=await repo.niche_leaders(
                    slug, platform=platform, metric=LeaderMetric.PRICE, limit=limit
                ),
            )
            watched = await repo.watchlist()
            dossier.watched = [item for item in watched if item.niche_slug == slug]
            return dossier

    async def search(self, query: str, *, limit: int = 20) -> list[ChannelProfile]:
        async with self._database.session() as session:
            return await CompetitorRepository(session).search_channels(query, limit=limit)

    async def watchlist(self) -> list[ChannelProfile]:
        async with self._database.session() as session:
            return await CompetitorRepository(session).watchlist()

    async def watch(
        self, channel_id: uuid.UUID, *, notes: str | None = None
    ) -> ChannelProfile | None:
        """Put a channel on the watchlist, or update the note if already there."""
        async with self._database.session() as session:
            channel = await session.get(MarketChannel, channel_id)
            if channel is None:
                return None

            existing = (
                await session.execute(
                    select(CompetitorWatch).where(
                        CompetitorWatch.market_channel_id == channel_id
                    )
                )
            ).scalar_one_or_none()

            if existing is None:
                session.add(
                    CompetitorWatch(
                        market_channel_id=channel_id,
                        niche_id=channel.niche_id,
                        notes=notes,
                    )
                )
            elif notes is not None:
                existing.notes = notes
            await session.commit()

            profile = await CompetitorRepository(session).channel_profile(str(channel_id))
        logger.info(
            "competitor watched",
            extra={"channel_id": str(channel_id), "channel": channel.channel_name},
        )
        return profile

    async def unwatch(self, channel_id: uuid.UUID) -> bool:
        async with self._database.session() as session:
            result = await session.execute(
                delete(CompetitorWatch).where(CompetitorWatch.market_channel_id == channel_id)
            )
            await session.commit()
        removed = bool(result.rowcount)
        if removed:
            logger.info("competitor unwatched", extra={"channel_id": str(channel_id)})
        return removed

    async def niche_exists(self, slug: str) -> bool:
        async with self._database.session() as session:
            return (
                await session.execute(select(Niche.id).where(Niche.slug == slug))
            ).scalar_one_or_none() is not None

    async def list_niche_slugs(self, *, limit: int = 60) -> list[tuple[str, str]]:
        """(slug, name) pairs, for error messages and discovery."""
        async with self._database.session() as session:
            rows = (
                await session.execute(select(Niche.slug, Niche.name).order_by(Niche.name))
            ).all()
        return [(slug, name) for slug, name in rows][:limit]
