"""Market channels and their metric history."""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import BigInteger, Date, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from channel_factory.core.enums import Platform
from channel_factory.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin


class MarketChannel(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A channel observed in market data (not one of our own channels).

    Identity is resolved through ``dedupe_key`` so the same channel appearing in
    several exports maps to one row with many snapshots, rather than to several
    rows that would distort niche-level statistics.
    """

    __tablename__ = "market_channels"
    __table_args__ = (UniqueConstraint("platform", "dedupe_key"),)

    platform: Mapped[Platform] = mapped_column(
        SAEnum(Platform, name="platform", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    dedupe_key: Mapped[str] = mapped_column(String(512), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    channel_name: Mapped[str] = mapped_column(String(512), nullable=False)
    channel_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    niche_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("niches.id", ondelete="SET NULL"), nullable=True, index=True
    )
    first_seen_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    last_seen_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    snapshots: Mapped[list[MarketSnapshot]] = relationship(
        back_populates="market_channel", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<MarketChannel {self.platform}:{self.channel_name!r}>"


class MarketSnapshot(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """Metrics for one channel at one point in time.

    Append-only: metrics are never updated in place, so the history of a channel
    (and of a niche) stays reconstructable.
    """

    __tablename__ = "market_snapshots"
    __table_args__ = (
        UniqueConstraint("market_channel_id", "direct_import_id", "snapshot_date"),
    )

    market_channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("market_channels.id", ondelete="CASCADE"), nullable=False, index=True
    )
    direct_import_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("direct_imports.id", ondelete="SET NULL"), nullable=True, index=True
    )
    snapshot_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    subscribers: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    err: Mapped[Decimal | None] = mapped_column(Numeric(10, 6), nullable=True)
    predicted_views: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    cpv: Mapped[Decimal | None] = mapped_column(Numeric(16, 6), nullable=True)
    campaign_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    region: Mapped[str | None] = mapped_column(String(255), nullable=True)
    extra: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    market_channel: Mapped[MarketChannel] = relationship(back_populates="snapshots")

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<MarketSnapshot {self.snapshot_date} channel={self.market_channel_id}>"
