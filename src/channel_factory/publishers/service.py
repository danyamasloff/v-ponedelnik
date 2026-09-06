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
from pathlib import Path

from sqlalchemy import select

from channel_factory.content.cards import CardContent, CardRenderError, render_card
from channel_factory.content.drafting import PostDraft, render_draft
from channel_factory.content.evergreen import EvergreenTopic, write_post
from channel_factory.content.generation import (
    AnalysisBrief,
    AnalysisGenerator,
    GeneratedAnalysis,
    GenerationError,
)
from channel_factory.core.enums import PublicationStatus, PublishMode
from channel_factory.core.logging import get_logger
from channel_factory.db.models import Publication, ResearchCluster
from channel_factory.db.repositories.research import ResearchClusterRepository
from channel_factory.db.session import Database
from channel_factory.publishers.base import (
    MediaItem,
    Publisher,
    PublishError,
    PublishRequest,
)

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
        generator: AnalysisGenerator | None = None,
        cards_dir: Path | None = None,
    ) -> None:
        self._database = database
        self._publisher = publisher
        self._channel_ref = channel_ref
        self._mode = mode
        self._auto_publish_enabled = auto_publish_enabled
        self._generator = generator
        self._cards_dir = cards_dir

    async def draft_for_cluster(self, cluster: ResearchCluster) -> PostDraft:
        """Build the skeleton for a cluster, including its primary source."""
        async with self._database.session() as session:
            sources = await ResearchClusterRepository(session).source_breakdown(cluster.id)

        primary = next((s for s in sources if s["role"] == "PRIMARY"), None) or (
            sources[0] if sources else None
        )
        generated = await self._analyse(cluster, primary)
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
            analysis=generated.text if generated else None,
            headline=generated.headline if generated else None,
            action=generated.action if generated else None,
        )

    async def _analyse(
        self, cluster: ResearchCluster, primary: dict | None
    ) -> GeneratedAnalysis | None:
        """Write the analysis paragraph, or leave the skeleton unfinished.

        A generation failure is never fatal: the draft stays a skeleton, the
        publisher refuses it, and the reason is visible. Publishing an
        unfinished post would be the worse failure.
        """
        if self._generator is None:
            return None
        brief = AnalysisBrief(
            title=cluster.canonical_title,
            vendor=cluster.vendor,
            product=cluster.product,
            version=cluster.version,
            event_type=cluster.event_type,
            source_url=primary["url"] if primary else None,
            source_name=primary["source"] if primary else None,
        )
        try:
            generated = await self._generator.analyse(brief)
        except GenerationError as exc:
            logger.warning(
                "content.generation.failed",
                extra={"cluster": str(cluster.id), "error": str(exc)},
            )
            return None
        logger.info(
            "content.generation.done",
            extra={"cluster": str(cluster.id), "backend": generated.backend},
        )
        return generated

    def _render_card(self, draft: PostDraft, cluster: ResearchCluster) -> MediaItem | None:
        """Draw the card for a post, or go without one.

        A missing font or an unreadable title costs us the picture, not the
        post: the text is what carries the value.
        """
        if self._cards_dir is None:
            return None
        try:
            path = render_card(
                CardContent(
                    title=cluster.canonical_title,
                    vendor=cluster.vendor,
                    kicker=cluster.vendor or "новости ИИ",
                    footer=draft.sources[0] if draft.sources else None,
                ),
                self._cards_dir / f"{cluster.id}.png",
            )
        except CardRenderError as exc:
            logger.warning(
                "content.card.failed", extra={"cluster": str(cluster.id), "error": str(exc)}
            )
            return None
        return MediaItem.from_path(path)

    async def publish_topic(self, topic: EvergreenTopic) -> PublishOutcome:
        """Write and publish one of our own posts.

        Same switches, same validation, same publications row as a news post —
        only the source of the text differs. Anything the generator cannot
        write is a BLOCKED row, never a half-post in the channel.
        """
        if self._generator is None:
            raise PublishError(
                "нет генератора текста: настройте Ollama, CONTENT_API_* или GEMINI_API_KEY"
            )
        post = await write_post(self._generator, topic)
        draft = PostDraft(
            platform=self._publisher.platform,
            text=post.text,
            title=post.headline,
            cluster_id="",
            sources=[],
            placeholders=[],
        )
        card = self._render_own_card(post.headline, topic.key)
        request = PublishRequest(
            text=draft.text,
            channel_ref=self._channel_ref or "",
            media=(card,) if card else (),
        )
        problems = self._publisher.validate(request)
        payload = self._publisher.build_payload(request)
        attachments = [{"type": card.kind.value, "source": card.label}] if card else []

        if problems:
            return await self._record(
                draft,
                payload,
                attachments=attachments,
                status=PublicationStatus.BLOCKED,
                reason="собственный пост не прошёл проверку",
                problems=problems,
                topic_key=topic.key,
            )
        if not self._auto_publish_enabled:
            return await self._record(
                draft,
                payload,
                attachments=attachments,
                status=PublicationStatus.BLOCKED,
                reason="AUTO_PUBLISH_ENABLED=false",
                topic_key=topic.key,
            )
        if self._mode is PublishMode.DRY_RUN:
            return await self._record(
                draft,
                payload,
                attachments=attachments,
                status=PublicationStatus.SIMULATED,
                reason="PUBLISH_MODE=DRY_RUN: ничего не отправлено",
                topic_key=topic.key,
            )

        try:
            result = await self._publisher.publish(request)
        except PublishError as exc:
            return await self._record(
                draft,
                payload,
                attachments=attachments,
                status=PublicationStatus.FAILED,
                reason=str(exc),
                topic_key=topic.key,
            )
        return await self._record(
            draft,
            payload,
            attachments=attachments,
            status=PublicationStatus.PUBLISHED,
            external_message_id=result.external_message_id,
            topic_key=topic.key,
        )

    def _render_own_card(self, headline: str, key: str) -> MediaItem | None:
        """Card for an own post: the headline is all it needs."""
        if self._cards_dir is None:
            return None
        try:
            path = render_card(
                CardContent(title=headline, vendor=key, kicker="разбор", footer=None),
                self._cards_dir / f"topic-{key}.png",
            )
        except CardRenderError as exc:
            logger.warning("content.card.failed", extra={"topic": key, "error": str(exc)})
            return None
        return MediaItem.from_path(path)

    async def publish_cluster(self, cluster_id: uuid.UUID) -> PublishOutcome:
        """Take one cluster all the way to the platform, or as far as allowed."""
        async with self._database.session() as session:
            cluster = await session.get(ResearchCluster, cluster_id)
            if cluster is None:
                raise PublishError(f"кластер {cluster_id} не найден")

        draft = await self.draft_for_cluster(cluster)
        card = self._render_card(draft, cluster) if draft.is_publishable else None
        request = PublishRequest(
            text=draft.text,
            channel_ref=self._channel_ref or "",
            media=(card,) if card else (),
        )
        problems = self._publisher.validate(request)
        payload = self._publisher.build_payload(request)
        # Descriptors, not tokens: this is stored and printed, and a card is
        # uploaded only when the post is actually sent.
        attachments = [{"type": card.kind.value, "source": card.label}] if card else []

        # Order matters: a skeleton is refused before the switches are even
        # consulted, so "publishing is enabled" can never mean "publish
        # anything".
        if not draft.is_publishable:
            return await self._record(
                draft,
                payload,
                attachments=attachments,
                status=PublicationStatus.BLOCKED,
                reason="черновик содержит незаполненные части (PHASE 5 ещё не построена)",
                problems=problems,
            )
        if problems:
            return await self._record(
                draft,
                payload,
                attachments=attachments,
                status=PublicationStatus.BLOCKED,
                reason="черновик не прошёл проверку",
                problems=problems,
            )
        if not self._auto_publish_enabled:
            return await self._record(
                draft,
                payload,
                attachments=attachments,
                status=PublicationStatus.BLOCKED,
                reason="AUTO_PUBLISH_ENABLED=false",
            )
        if self._mode is PublishMode.DRY_RUN:
            return await self._record(
                draft,
                payload,
                attachments=attachments,
                status=PublicationStatus.SIMULATED,
                reason="PUBLISH_MODE=DRY_RUN: ничего не отправлено",
            )

        try:
            result = await self._publisher.publish(request)
        except PublishError as exc:
            return await self._record(
                draft,
                payload,
                attachments=attachments,
                status=PublicationStatus.FAILED,
                reason=str(exc),
            )

        return await self._record(
            draft,
            payload,
            attachments=attachments,
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
        attachments: list[dict] | None = None,
        topic_key: str | None = None,
    ) -> PublishOutcome:
        async with self._database.session() as session:
            publication = Publication(
                platform=self._publisher.platform,
                mode=self._mode,
                status=status,
                research_cluster_id=uuid.UUID(draft.cluster_id) if draft.cluster_id else None,
                topic_key=topic_key,
                channel_ref=self._channel_ref or "-",
                external_message_id=external_message_id,
                text=draft.text,
                text_length=draft.length,
                attachments=attachments or [],
                request_payload=payload,
                published_at=(datetime.now(UTC) if status is PublicationStatus.PUBLISHED else None),
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
                        select(Publication).order_by(Publication.created_at.desc()).limit(limit)
                    )
                )
                .scalars()
                .all()
            )
