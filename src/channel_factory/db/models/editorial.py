"""Durable editorial decisions and per-destination delivery state."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from channel_factory.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin


class ContentItem(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "content_items"
    __table_args__ = (UniqueConstraint("brand", "event_key"),)
    brand: Mapped[str] = mapped_column(String(80), default="ai-skills")
    event_key: Mapped[str] = mapped_column(String(512))
    cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("research_clusters.id", ondelete="RESTRICT"), nullable=True
    )
    title: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(40), default="DRAFT")
    reason: Mapped[str | None] = mapped_column(Text)


class ContentVersion(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "content_versions"
    content_item_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("content_items.id", ondelete="RESTRICT"), index=True
    )
    # Immutable canonical body; the first release uses identical plain-text adaptations.
    body: Mapped[str] = mapped_column(Text)
    body_hash: Mapped[str] = mapped_column(String(64), index=True)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    generation: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)


class VerificationRun(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "verification_runs"
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("content_versions.id", ondelete="RESTRICT"), index=True
    )
    status: Mapped[str] = mapped_column(String(40))
    body_hash: Mapped[str] = mapped_column(String(64))
    policy_version: Mapped[str] = mapped_column(String(64), default="editorial_v1")
    # Claim -> evidence index -> exact supporting excerpt, including failed checks.
    claims: Mapped[dict[str, Any]] = mapped_column(JSONB)


class Publication(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    __tablename__ = "publications"
    __table_args__ = (UniqueConstraint("version_id", "platform", "channel_id"),)
    version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("content_versions.id", ondelete="RESTRICT"), index=True
    )
    platform: Mapped[str] = mapped_column(String(16))
    channel_id: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), default="QUEUED")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    external_id: Mapped[str | None] = mapped_column(String(255))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    simulated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)


class PublishingControl(Base, TimestampMixin):
    __tablename__ = "publishing_control"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    paused: Mapped[bool] = mapped_column(default=True)
    reason: Mapped[str] = mapped_column(Text, default="Initial dry run")


class PublishingAudit(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    __tablename__ = "publishing_audit"
    action: Mapped[str] = mapped_column(String(40))
    details: Mapped[dict[str, Any]] = mapped_column(JSONB)
