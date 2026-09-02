"""Niche (category) taxonomy."""

from __future__ import annotations

from sqlalchemy import String, Text
from sqlalchemy.orm import Mapped, mapped_column

from channel_factory.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Niche(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A market category channels are grouped by.

    Created on demand while importing market data: the category label from the
    source file is slugified, and the original spelling is preserved in
    ``raw_label`` so nothing from the source is lost.
    """

    __tablename__ = "niches"

    slug: Mapped[str] = mapped_column(String(160), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    raw_label: Mapped[str | None] = mapped_column(String(255), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<Niche slug={self.slug!r}>"
