"""Import history: one row per import run, one row per source file line."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import BigInteger, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from channel_factory.core.enums import ImportStatus, RowStatus
from channel_factory.db.base import Base, CreatedAtMixin, TimestampMixin, UUIDPrimaryKeyMixin


class DirectImport(Base, UUIDPrimaryKeyMixin, TimestampMixin):
    """A single import run of one market-data file.

    ``created_at`` marks the start of the run, ``finished_at`` its completion.
    Runs are append-only: re-importing the same file with ``--force`` creates a
    new row rather than overwriting the previous one.
    """

    __tablename__ = "direct_imports"

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    file_checksum: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[ImportStatus] = mapped_column(
        SAEnum(ImportStatus, name="import_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    forced: Mapped[bool] = mapped_column(nullable=False, default=False)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    valid_row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rejected_row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    warning_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    channels_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    snapshots_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    column_mapping: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    rows: Mapped[list[DirectImportRow]] = relationship(
        back_populates="direct_import", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<DirectImport {self.original_filename!r} status={self.status}>"


class DirectImportRow(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One source-file line, kept in both raw and normalized form.

    Rows are immutable audit records: ``raw_data`` preserves the source values
    verbatim, ``normalized_data`` holds the parsed result, and rejected rows are
    stored too (with their errors) so nothing is dropped silently.
    """

    __tablename__ = "direct_import_rows"

    direct_import_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("direct_imports.id", ondelete="CASCADE"), nullable=False, index=True
    )
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[RowStatus] = mapped_column(
        SAEnum(RowStatus, name="row_status", values_callable=lambda e: [m.value for m in e]),
        nullable=False,
    )
    raw_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    normalized_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    market_channel_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("market_channels.id", ondelete="SET NULL"), nullable=True, index=True
    )
    errors: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    warnings: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)

    direct_import: Mapped[DirectImport] = relationship(back_populates="rows")

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<DirectImportRow #{self.row_number} status={self.status}>"
