"""What we published, or would have published.

A dry run writes rows here exactly like a live run does, differing only in
``mode`` and ``status``. That is deliberate: the rehearsal has to exercise the
same code path and leave the same audit trail, otherwise it rehearses nothing.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from channel_factory.core.enums import Platform, PublicationStatus, PublishMode
from channel_factory.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


def _enum(enum_type: type, name: str) -> SAEnum:
    return SAEnum(enum_type, name=name, values_callable=lambda e: [m.value for m in e])


class Publication(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """One post sent to a platform, or simulated against it."""

    __tablename__ = "publications"
    __table_args__ = (
        Index("ix_publications_platform_created", "platform", "created_at"),
        Index("ix_publications_cluster", "research_cluster_id"),
    )

    platform: Mapped[Platform] = mapped_column(
        _enum(Platform, "platform"), nullable=False
    )
    mode: Mapped[PublishMode] = mapped_column(_enum(PublishMode, "publish_mode"), nullable=False)
    status: Mapped[PublicationStatus] = mapped_column(
        _enum(PublicationStatus, "publication_status"), nullable=False
    )

    # Kept as a plain reference rather than a hard link: a publication is a
    # historical fact and must survive the research data being pruned.
    research_cluster_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("research_clusters.id", ondelete="SET NULL"), nullable=True
    )

    channel_ref: Mapped[str] = mapped_column(String(128), nullable=False)

    # Own posts have no research cluster, so this is what says "already
    # published" for them. Null for everything driven by research.
    topic_key: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    external_message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)

    text: Mapped[str] = mapped_column(Text, nullable=False)
    text_length: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attachments: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    # The exact payload that was sent, or would have been sent. This is what
    # makes a dry run reviewable rather than merely reassuring.
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<Publication {self.platform} {self.status} {self.mode}>"
