"""Channels we deliberately track as competitors.

There is no separate snapshot table for competitors: a competitor *is* a
:class:`MarketChannel`, and its metric history already lives in
``market_snapshots``. Watching one only adds intent — "this channel is a
reference point for us" — plus the niche it is watched in and a note.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

from sqlalchemy import ForeignKey, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from channel_factory.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin

if TYPE_CHECKING:
    from channel_factory.db.models.market import MarketChannel
    from channel_factory.db.models.niche import Niche


class CompetitorWatch(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A market channel put on the competitor watchlist."""

    __tablename__ = "competitor_watchlist"
    __table_args__ = (UniqueConstraint("market_channel_id"),)

    # No index=True here: the unique constraint below already provides one.
    market_channel_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("market_channels.id", ondelete="CASCADE"), nullable=False
    )
    # The niche this channel is watched *for*, which need not be the channel's
    # own category: a strong neighbouring-niche channel can still be a reference.
    niche_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("niches.id", ondelete="SET NULL"), nullable=True, index=True
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    market_channel: Mapped[MarketChannel] = relationship()
    niche: Mapped[Niche | None] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<CompetitorWatch channel={self.market_channel_id}>"
