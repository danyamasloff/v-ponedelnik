"""Registry of data sources (market exports, catalogs, future content references)."""

from __future__ import annotations

from sqlalchemy import Enum as SAEnum
from sqlalchemy import String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from channel_factory.core.enums import SourceKind
from channel_factory.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Source(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """Where a batch of data came from.

    One generic table serves market data today and fact-check references later,
    discriminated by :class:`SourceKind`.
    """

    __tablename__ = "sources"
    __table_args__ = (UniqueConstraint("kind", "name"),)

    kind: Mapped[SourceKind] = mapped_column(
        SAEnum(SourceKind, name="source_kind", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<Source kind={self.kind} name={self.name!r}>"
