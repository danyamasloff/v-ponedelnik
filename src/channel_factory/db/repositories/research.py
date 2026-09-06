"""Repository for the research engine."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from channel_factory.core.enums import (
    RESCORABLE_CLUSTER_STATUSES,
    ClusterStatus,
    ResearchItemStatus,
)
from channel_factory.db.models import (
    ResearchCluster,
    ResearchClusterItem,
    ResearchItem,
    ResearchSource,
    TopicScore,
)


class ResearchSourceRepository:
    """Whitelisted sources."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def by_key(self, key: str) -> ResearchSource | None:
        return (
            await self._session.execute(select(ResearchSource).where(ResearchSource.key == key))
        ).scalar_one_or_none()

    async def all(self, *, enabled_only: bool = False) -> list[ResearchSource]:
        query = select(ResearchSource).order_by(ResearchSource.key)
        if enabled_only:
            query = query.where(ResearchSource.enabled.is_(True))
        return list((await self._session.execute(query)).scalars().all())


class ResearchItemRepository:
    """Discovered documents."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def by_natural_key(
        self, source_id: uuid.UUID, external_id: str
    ) -> ResearchItem | None:
        """The provider's own identity for an item — the primary lookup."""
        return (
            await self._session.execute(
                select(ResearchItem).where(
                    ResearchItem.research_source_id == source_id,
                    ResearchItem.external_id == external_id,
                )
            )
        ).scalar_one_or_none()

    async def unclustered(self, limit: int = 500) -> list[ResearchItem]:
        return list(
            (
                await self._session.execute(
                    select(ResearchItem)
                    .where(ResearchItem.status == ResearchItemStatus.NEW)
                    .order_by(ResearchItem.discovered_at)
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    async def clustered_since(self, since: datetime, limit: int = 2000) -> list[ResearchItem]:
        """Recent clustered items — the candidate pool for deduplication."""
        return list(
            (
                await self._session.execute(
                    select(ResearchItem)
                    .where(
                        ResearchItem.status != ResearchItemStatus.NEW,
                        ResearchItem.discovered_at >= since,
                    )
                    .order_by(ResearchItem.discovered_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    async def counts_by_status(self) -> dict[ResearchItemStatus, int]:
        rows = await self._session.execute(
            select(ResearchItem.status, func.count()).group_by(ResearchItem.status)
        )
        return dict(rows.all())  # type: ignore[arg-type]


class ResearchClusterRepository:
    """Information events."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def by_key(self, cluster_key: str) -> ResearchCluster | None:
        return (
            await self._session.execute(
                select(ResearchCluster).where(ResearchCluster.cluster_key == cluster_key)
            )
        ).scalar_one_or_none()

    async def by_id(self, cluster_id: uuid.UUID) -> ResearchCluster | None:
        return await self._session.get(ResearchCluster, cluster_id)

    async def cluster_of_item(self, item_id: uuid.UUID) -> ResearchCluster | None:
        return (
            await self._session.execute(
                select(ResearchCluster)
                .join(ResearchClusterItem)
                .where(ResearchClusterItem.research_item_id == item_id)
                .limit(1)
            )
        ).scalar_one_or_none()

    async def needing_score(self, limit: int = 200) -> list[ResearchCluster]:
        """Clusters whose score is missing or stale.

        A cluster that gained supporting items is re-scored, which is what makes
        FILTERED and BACKLOG reversible rather than final.
        """
        return list(
            (
                await self._session.execute(
                    select(ResearchCluster)
                    .where(
                        ResearchCluster.needs_rescore.is_(True),
                        ResearchCluster.status.in_(RESCORABLE_CLUSTER_STATUSES),
                    )
                    .order_by(ResearchCluster.last_seen_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

    async def top(
        self, *, statuses: list[ClusterStatus] | None = None, limit: int = 20
    ) -> list[ResearchCluster]:
        query = select(ResearchCluster)
        if statuses:
            query = query.where(ResearchCluster.status.in_(statuses))
        query = query.order_by(
            ResearchCluster.topic_score.desc().nulls_last(),
            ResearchCluster.last_seen_at.desc(),
        ).limit(limit)
        return list((await self._session.execute(query)).scalars().all())

    async def with_items(self, cluster_id: uuid.UUID) -> ResearchCluster | None:
        return (
            await self._session.execute(
                select(ResearchCluster)
                .where(ResearchCluster.id == cluster_id)
                .options(
                    selectinload(ResearchCluster.items).selectinload(ResearchClusterItem.item)
                )
            )
        ).scalar_one_or_none()

    async def counts_by_status(self) -> dict[ClusterStatus, int]:
        rows = await self._session.execute(
            select(ResearchCluster.status, func.count()).group_by(ResearchCluster.status)
        )
        return dict(rows.all())  # type: ignore[arg-type]

    async def latest_score(self, cluster_id: uuid.UUID) -> TopicScore | None:
        return (
            await self._session.execute(
                select(TopicScore)
                .where(TopicScore.research_cluster_id == cluster_id)
                .order_by(TopicScore.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

    async def source_breakdown(self, cluster_id: uuid.UUID) -> list[dict[str, Any]]:
        """Which sources contributed to a cluster, with their trust."""
        rows = await self._session.execute(
            select(
                ResearchSource.name,
                ResearchItem.trust,
                ResearchClusterItem.role,
                ResearchClusterItem.matched_by,
                ResearchItem.url,
                ResearchItem.title,
            )
            .join(ResearchClusterItem, ResearchClusterItem.research_item_id == ResearchItem.id)
            .join(ResearchSource, ResearchSource.id == ResearchItem.research_source_id)
            .where(ResearchClusterItem.research_cluster_id == cluster_id)
            .order_by(ResearchClusterItem.role)
        )
        return [
            {
                "source": name,
                "trust": trust.value,
                "role": role.value,
                "matched_by": matched_by.value,
                "url": url,
                "title": title,
            }
            for name, trust, role, matched_by, url, title in rows.all()
        ]
