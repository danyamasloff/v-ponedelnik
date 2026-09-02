"""Repositories for sources and import history."""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from channel_factory.core.enums import ImportStatus, RowStatus, SourceKind
from channel_factory.db.models import DirectImport, DirectImportRow, Source

# An import that ended in these states counts as "this file is already in".
COMPLETED_STATUSES = (ImportStatus.SUCCESS, ImportStatus.PARTIAL)


class SourceRepository:
    """Lookup and creation of data sources."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_or_create(self, kind: SourceKind, name: str) -> Source:
        result = await self._session.execute(
            select(Source).where(Source.kind == kind, Source.name == name)
        )
        source = result.scalar_one_or_none()
        if source is not None:
            return source
        source = Source(kind=kind, name=name)
        self._session.add(source)
        await self._session.flush()
        return source


class DirectImportRepository:
    """Import runs and their rows."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def find_completed_by_checksum(self, checksum: str) -> DirectImport | None:
        """Return the most recent completed import of a file with this checksum."""
        result = await self._session.execute(
            select(DirectImport)
            .where(
                DirectImport.file_checksum == checksum,
                DirectImport.status.in_(COMPLETED_STATUSES),
            )
            .order_by(DirectImport.created_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def get(self, import_id: uuid.UUID) -> DirectImport | None:
        return await self._session.get(DirectImport, import_id)

    async def list_recent(self, limit: int = 20) -> list[DirectImport]:
        result = await self._session.execute(
            select(DirectImport).order_by(DirectImport.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())

    async def count_rows_by_status(self, import_id: uuid.UUID) -> dict[RowStatus, int]:
        result = await self._session.execute(
            select(DirectImportRow.status, func.count())
            .where(DirectImportRow.direct_import_id == import_id)
            .group_by(DirectImportRow.status)
        )
        return dict(result.all())  # type: ignore[arg-type]

    async def sample_rows(
        self,
        import_id: uuid.UUID,
        *,
        status: RowStatus | None = None,
        with_warnings: bool = False,
        limit: int = 10,
    ) -> list[DirectImportRow]:
        """Return a few rows for CLI inspection."""
        query = select(DirectImportRow).where(DirectImportRow.direct_import_id == import_id)
        if status is not None:
            query = query.where(DirectImportRow.status == status)
        if with_warnings:
            query = query.where(func.jsonb_array_length(DirectImportRow.warnings) > 0)
        query = query.order_by(DirectImportRow.row_number).limit(limit)
        result = await self._session.execute(query)
        return list(result.scalars().all())

    async def insert_rows(self, rows: list[dict[str, Any]], *, chunk_size: int = 1000) -> None:
        """Batch-insert import rows (never one INSERT per row)."""
        if not rows:
            return
        from sqlalchemy import insert

        for start in range(0, len(rows), chunk_size):
            await self._session.execute(insert(DirectImportRow), rows[start : start + chunk_size])
