"""Research engine: sources, items, clusters and topic scores.

Identity rules, which the deduplication design depends on:

* ``(research_source_id, external_id)`` is the natural key. Providers that give
  no id of their own fall back to the canonical URL, so the pair always exists.
* ``url_hash`` is deliberately **not** unique: the same story legitimately
  appears at the same URL across sources, and the hash is a clustering signal,
  not an identity.
* ``content_fingerprint`` detects that a page kept its URL but changed its
  content — the normal case for changelogs and documentation.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from channel_factory.core.enums import (
    ClusterItemRole,
    ClusterStatus,
    EventType,
    MatchMethod,
    RejectionReason,
    ResearchItemStatus,
    ResearchProviderType,
    TrustLevel,
    VerificationStatus,
)
from channel_factory.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin


def _enum(enum_type: type, name: str) -> SAEnum:
    return SAEnum(enum_type, name=name, values_callable=lambda e: [m.value for m in e])


class ResearchSource(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A whitelisted source the engine polls."""

    __tablename__ = "research_sources"
    __table_args__ = (UniqueConstraint("key"),)

    key: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    provider: Mapped[ResearchProviderType] = mapped_column(
        _enum(ResearchProviderType, "research_provider_type"), nullable=False
    )
    trust: Mapped[TrustLevel] = mapped_column(_enum(TrustLevel, "trust_level"), nullable=False)
    url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    repo: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    poll_interval_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    last_polled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_success_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    consecutive_failures: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    items: Mapped[list[ResearchItem]] = relationship(
        back_populates="source", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<ResearchSource {self.key} ({self.provider})>"


class ResearchItem(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One document discovered at a source."""

    __tablename__ = "research_items"
    __table_args__ = (
        UniqueConstraint("research_source_id", "external_id"),
        Index("ix_research_items_url_hash", "url_hash"),
        Index("ix_research_items_status_discovered", "status", "discovered_at"),
        Index("ix_research_items_simhash", "simhash"),
    )

    research_source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_sources.id", ondelete="CASCADE"), nullable=False, index=True
    )
    source_type: Mapped[ResearchProviderType] = mapped_column(
        _enum(ResearchProviderType, "research_provider_type"), nullable=False
    )
    trust: Mapped[TrustLevel] = mapped_column(_enum(TrustLevel, "trust_level"), nullable=False)
    # Provider identity: the feed's guid, the release id, or the canonical URL
    # when the provider offers nothing better.
    external_id: Mapped[str] = mapped_column(String(512), nullable=False)

    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    canonical_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    url_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    title: Mapped[str] = mapped_column(String(1024), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # A bounded excerpt only — full third-party texts are never stored.
    content_excerpt: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Detects "same URL, new content" without keeping the content itself.
    content_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    # Stored as a signed 64-bit integer so PostgreSQL can hold it in a bigint.
    # Hamming distance is computed in Python over a bounded candidate window
    # rather than in SQL: at a few hundred items a day that is instant, and it
    # keeps the schema free of banding columns we would not otherwise need.
    simhash: Mapped[int | None] = mapped_column(BigInteger, nullable=True)

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    author: Mapped[str | None] = mapped_column(String(255), nullable=True)
    publisher: Mapped[str | None] = mapped_column(String(255), nullable=True)
    language: Mapped[str | None] = mapped_column(String(8), nullable=True)
    topic: Mapped[str | None] = mapped_column(String(128), nullable=True)
    subtopic: Mapped[str | None] = mapped_column(String(128), nullable=True)
    entities: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    raw_metadata: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    relevance_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    novelty_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    practical_value_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    authority_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)

    verification_status: Mapped[VerificationStatus] = mapped_column(
        _enum(VerificationStatus, "verification_status"),
        nullable=False,
        default=VerificationStatus.UNVERIFIED,
    )
    status: Mapped[ResearchItemStatus] = mapped_column(
        _enum(ResearchItemStatus, "research_item_status"),
        nullable=False,
        default=ResearchItemStatus.NEW,
    )
    rejection_reason: Mapped[RejectionReason | None] = mapped_column(
        _enum(RejectionReason, "rejection_reason"), nullable=True
    )

    source: Mapped[ResearchSource] = relationship(back_populates="items")
    cluster_links: Mapped[list[ResearchClusterItem]] = relationship(
        back_populates="item", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<ResearchItem {self.title[:40]!r} status={self.status}>"


class ResearchCluster(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One information event, however many sources reported it."""

    __tablename__ = "research_clusters"
    __table_args__ = (
        UniqueConstraint("cluster_key"),
        Index("ix_research_clusters_status_score", "status", "topic_score"),
    )

    cluster_key: Mapped[str] = mapped_column(String(512), nullable=False)
    event_type: Mapped[EventType] = mapped_column(
        _enum(EventType, "event_type"), nullable=False, default=EventType.OTHER
    )
    canonical_title: Mapped[str] = mapped_column(String(1024), nullable=False)
    canonical_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    primary_item_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("research_items.id", ondelete="SET NULL"), nullable=True
    )

    vendor: Mapped[str | None] = mapped_column(String(128), nullable=True)
    product: Mapped[str | None] = mapped_column(String(128), nullable=True)
    version: Mapped[str | None] = mapped_column(String(64), nullable=True)

    event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_trust: Mapped[TrustLevel] = mapped_column(
        _enum(TrustLevel, "trust_level"), nullable=False, default=TrustLevel.UNKNOWN
    )
    topic_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    score_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    scored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set when new supporting items arrive after scoring, so a re-score run
    # knows exactly what to recompute instead of rescoring everything.
    needs_rescore: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    status: Mapped[ClusterStatus] = mapped_column(
        _enum(ClusterStatus, "cluster_status"), nullable=False, default=ClusterStatus.OPEN
    )
    rejection_reason: Mapped[RejectionReason | None] = mapped_column(
        _enum(RejectionReason, "rejection_reason"), nullable=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    items: Mapped[list[ResearchClusterItem]] = relationship(
        back_populates="cluster", cascade="all, delete-orphan", passive_deletes=True
    )
    scores: Mapped[list[TopicScore]] = relationship(
        back_populates="cluster", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<ResearchCluster {self.cluster_key!r} score={self.topic_score}>"


class ResearchClusterItem(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Membership of an item in a cluster, with how it was matched."""

    __tablename__ = "research_cluster_items"
    __table_args__ = (UniqueConstraint("research_cluster_id", "research_item_id"),)

    research_cluster_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_clusters.id", ondelete="CASCADE"), nullable=False, index=True
    )
    research_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_items.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[ClusterItemRole] = mapped_column(
        _enum(ClusterItemRole, "cluster_item_role"),
        nullable=False,
        default=ClusterItemRole.SUPPORTING,
    )
    matched_by: Mapped[MatchMethod] = mapped_column(
        _enum(MatchMethod, "match_method"), nullable=False
    )
    similarity: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)

    cluster: Mapped[ResearchCluster] = relationship(back_populates="items")
    item: Mapped[ResearchItem] = relationship(back_populates="cluster_links")

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<ResearchClusterItem role={self.role} by={self.matched_by}>"


class TopicScore(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """A versioned score for one cluster.

    Append-only, like ``niche_scores``: re-scoring a cluster adds a row rather
    than editing the previous one, so a past selection stays explainable.
    """

    __tablename__ = "topic_scores"
    __table_args__ = (
        Index("ix_topic_scores_cluster_created", "research_cluster_id", "created_at"),
    )

    research_cluster_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("research_clusters.id", ondelete="CASCADE"), nullable=False
    )
    score_version: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    decision: Mapped[ClusterStatus] = mapped_column(
        _enum(ClusterStatus, "cluster_status"), nullable=False
    )
    components: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    inputs: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    item_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    as_of_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    cluster: Mapped[ResearchCluster] = relationship(back_populates="scores")

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<TopicScore {self.score_version} score={self.score} -> {self.decision}>"
