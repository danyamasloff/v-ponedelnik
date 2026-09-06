"""Publishing with a kill switch.

Two independent switches must both allow a post before anything leaves the
machine, and the default of each is "no":

* ``PUBLISH_MODE`` — ``DRY_RUN`` records what would have been sent and calls
  nothing; ``LIVE`` actually posts.
* ``AUTO_PUBLISH_ENABLED`` — the operator's master switch.

A dry run travels the same code path, runs the same validation and writes the
same row; only the final HTTP call is replaced. A rehearsal that skips the
interesting parts proves nothing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from channel_factory.content.drafting import PostDraft, render_draft
from channel_factory.core.enums import PublicationStatus, PublishMode
from channel_factory.core.logging import get_logger
from channel_factory.db.models import Publication, ResearchCluster
from channel_factory.db.repositories.research import ResearchClusterRepository
from channel_factory.db.session import Database
from channel_factory.publishers.base import Publisher, PublishError, PublishRequest

logger = get_logger(__name__)


@dataclass
class PublishOutcome:
    """What happened to one post."""

    status: PublicationStatus
    mode: PublishMode
    draft: PostDraft
    publication_id: uuid.UUID | None = None
    external_message_id: str | None = None
    problems: list[str] | None = None
    reason: str | None = None


class PublishingService:
    """Renders, checks and (only if allowed) sends posts."""

    def __init__(
        self,
        database: Database,
        publisher: Publisher,
        *,
        channel_ref: str | None,
        mode: PublishMode = PublishMode.DRY_RUN,
        auto_publish_enabled: bool = False,
    ) -> None:
        self._database = database
        self._publisher = publisher
        self._channel_ref = channel_ref
        self._mode = mode
        self._auto_publish_enabled = auto_publish_enabled

    async def draft_for_cluster(self, cluster: ResearchCluster) -> PostDraft:
        """Build the skeleton for a cluster, including its primary source."""
        async with self._database.session() as session:
            sources = await ResearchClusterRepository(session).source_breakdown(cluster.id)

        primary = next((s for s in sources if s["role"] == "PRIMARY"), None) or (
            sources[0] if sources else None
        )
        return render_draft(
            platform=self._publisher.platform,
            cluster_id=str(cluster.id),
            title=cluster.canonical_title,
            vendor=cluster.vendor,
            product=cluster.product,
            version=cluster.version,
            event_type=cluster.event_type,
            primary_url=primary["url"] if primary else None,
            primary_source=primary["source"] if primary else None,
            trust=primary["trust"] if primary else None,
            text_limit=self._publisher.text_limit,
        )

    async def publish_cluster(self, cluster_id: uuid.UUID) -> PublishOutcome:
        """Take one cluster all the way to the platform, or as far as allowed."""
        async with self._database.session() as session:
            cluster = await session.get(ResearchCluster, cluster_id)
            if cluster is None:
                raise PublishError(f"кластер {cluster_id} не найден")

        draft = await self.draft_for_cluster(cluster)
        request = PublishRequest(text=draft.text, channel_ref=self._channel_ref or "")
        problems = self._publisher.validate(request)
        payload = self._publisher.build_payload(request)

        # Order matters: a skeleton is refused before the switches are even
        # consulted, so "publishing is enabled" can never mean "publish
        # anything".
        if not draft.is_publishable:
            return await self._record(
                draft,
                payload,
                status=PublicationStatus.BLOCKED,
                reason="черновик содержит незаполненные части (PHASE 5 ещё не построена)",
                problems=problems,
            )
        if problems:
            return await self._record(
                draft,
                payload,
                status=PublicationStatus.BLOCKED,
                reason="черновик не прошёл проверку",
                problems=problems,
            )
        if not self._auto_publish_enabled:
            return await self._record(
                draft,
                payload,
                status=PublicationStatus.BLOCKED,
                reason="AUTO_PUBLISH_ENABLED=false",
            )
        if self._mode is PublishMode.DRY_RUN:
            return await self._record(
                draft,
                payload,
                status=PublicationStatus.SIMULATED,
                reason="PUBLISH_MODE=DRY_RUN: ничего не отправлено",
            )

        try:
            result = await self._publisher.publish(request)
        except PublishError as exc:
            return await self._record(
                draft, payload, status=PublicationStatus.FAILED, reason=str(exc)
            )

        return await self._record(
            draft,
            payload,
            status=PublicationStatus.PUBLISHED,
            external_message_id=result.external_message_id,
        )

    async def _record(
        self,
        draft: PostDraft,
        payload: dict,
        *,
        status: PublicationStatus,
        reason: str | None = None,
        problems: list[str] | None = None,
        external_message_id: str | None = None,
    ) -> PublishOutcome:
        async with self._database.session() as session:
            publication = Publication(
                platform=self._publisher.platform,
                mode=self._mode,
                status=status,
                research_cluster_id=uuid.UUID(draft.cluster_id),
                channel_ref=self._channel_ref or "-",
                external_message_id=external_message_id,
                text=draft.text,
                text_length=draft.length,
                attachments=[],
                request_payload=payload,
                published_at=(
                    datetime.now(UTC) if status is PublicationStatus.PUBLISHED else None
                ),
                error_message=reason,
            )
            session.add(publication)
            await session.commit()
            publication_id = publication.id

        logger.info(
            "publication recorded",
            extra={
                "platform": self._publisher.platform.value,
                "status": status.value,
                "mode": self._mode.value,
            },
        )
        return PublishOutcome(
            status=status,
            mode=self._mode,
            draft=draft,
            publication_id=publication_id,
            external_message_id=external_message_id,
            problems=problems,
            reason=reason,
        )

    async def recent(self, limit: int = 20) -> list[Publication]:
        async with self._database.session() as session:
            return list(
                (
                    await session.execute(
                        select(Publication)
                        .order_by(Publication.created_at.desc())
                        .limit(limit)
                    )
                )
                .scalars()
                .all()
            )
