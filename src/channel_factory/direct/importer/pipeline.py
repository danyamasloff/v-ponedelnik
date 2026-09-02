"""Import pipeline: file -> validated rows -> PostgreSQL.

Transaction strategy: the import record is committed *before* processing starts
(status ``RUNNING``) so that a failure leaves a durable audit trail instead of
vanishing with the rollback. Row and snapshot writes then happen in a second
transaction, and the record is finalized in a third.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from channel_factory.core.enums import ImportStatus, Platform, RowStatus, SourceKind
from channel_factory.core.logging import get_logger
from channel_factory.db.models import DirectImport, MarketChannel
from channel_factory.db.repositories.imports import DirectImportRepository, SourceRepository
from channel_factory.db.repositories.market import (
    MarketChannelRepository,
    MarketSnapshotRepository,
    NicheRepository,
)
from channel_factory.db.session import Database
from channel_factory.direct.importer.checksum import file_sha256
from channel_factory.direct.importer.reader import SourceFileError, SourceTable, read_table
from channel_factory.direct.importer.row_mapper import NormalizedRow, normalize_row
from channel_factory.direct.mapping.loader import (
    ColumnMappingConfig,
    MappedColumns,
    map_columns,
)

logger = get_logger(__name__)

DEFAULT_SOURCE_NAME = "Yandex Direct export"


@dataclass
class ImportOutcome:
    """Everything the CLI needs to report about one import attempt."""

    status: ImportStatus
    import_id: uuid.UUID | None = None
    skipped: bool = False
    duplicate_of: uuid.UUID | None = None
    duplicate_imported_at: datetime | None = None
    row_count: int = 0
    valid_row_count: int = 0
    rejected_row_count: int = 0
    warning_count: int = 0
    channels_created: int = 0
    snapshots_created: int = 0
    duplicates_in_file: int = 0
    unmapped_headers: tuple[str, ...] = ()
    missing_required: tuple[str, ...] = ()
    error_message: str | None = None
    duration_ms: int = 0
    warning_samples: list[str] = field(default_factory=list)


class DirectImportService:
    """Imports market-data exports into PostgreSQL."""

    def __init__(self, database: Database, mapping_config: ColumnMappingConfig) -> None:
        self._database = database
        self._mapping = mapping_config

    async def import_file(
        self,
        path: Path,
        *,
        force: bool = False,
        source_name: str = DEFAULT_SOURCE_NAME,
        snapshot_date: date | None = None,
        sheet: str | None = None,
    ) -> ImportOutcome:
        """Import one XLSX/CSV file. Returns an outcome instead of raising for
        expected failures (unreadable file, unmappable columns)."""
        started = time.perf_counter()
        checksum = await asyncio.to_thread(file_sha256, path)
        file_size = path.stat().st_size
        default_date = snapshot_date or datetime.now(UTC).date()

        async with self._database.session() as session:
            existing = await DirectImportRepository(session).find_completed_by_checksum(checksum)
            if existing is not None and not force:
                logger.info(
                    "import skipped: file already imported",
                    extra={
                        "file": path.name,
                        "checksum": checksum[:12],
                        "import_id": str(existing.id),
                    },
                )
                return ImportOutcome(
                    status=existing.status,
                    skipped=True,
                    duplicate_of=existing.id,
                    duplicate_imported_at=existing.created_at,
                    duration_ms=int((time.perf_counter() - started) * 1000),
                )

        import_id = await self._create_import_record(
            path=path,
            checksum=checksum,
            file_size=file_size,
            source_name=source_name,
            forced=force,
        )
        logger.info(
            "import started",
            extra={"file": path.name, "import_id": str(import_id), "forced": force},
        )

        try:
            outcome = await self._run(
                import_id=import_id,
                path=path,
                sheet=sheet,
                default_date=default_date,
            )
        except SourceFileError as exc:
            await self._fail_import(import_id, str(exc))
            return ImportOutcome(
                status=ImportStatus.FAILED,
                import_id=import_id,
                error_message=str(exc),
                duration_ms=int((time.perf_counter() - started) * 1000),
            )
        except Exception as exc:
            await self._fail_import(import_id, f"unexpected error: {exc}")
            raise

        outcome.duration_ms = int((time.perf_counter() - started) * 1000)
        logger.info(
            "import finished",
            extra={
                "import_id": str(import_id),
                "status": outcome.status.value,
                "rows": outcome.row_count,
                "valid": outcome.valid_row_count,
                "rejected": outcome.rejected_row_count,
                "warnings": outcome.warning_count,
                "duration_ms": outcome.duration_ms,
            },
        )
        return outcome

    async def _create_import_record(
        self,
        *,
        path: Path,
        checksum: str,
        file_size: int,
        source_name: str,
        forced: bool,
    ) -> uuid.UUID:
        async with self._database.session() as session:
            source = await SourceRepository(session).get_or_create(
                SourceKind.DIRECT_EXPORT, source_name
            )
            record = DirectImport(
                source_id=source.id,
                original_filename=path.name,
                file_checksum=checksum,
                file_size=file_size,
                status=ImportStatus.RUNNING,
                forced=forced,
            )
            session.add(record)
            await session.commit()
            return record.id

    async def _fail_import(self, import_id: uuid.UUID, message: str) -> None:
        async with self._database.session() as session:
            record = await session.get(DirectImport, import_id)
            if record is not None:
                record.status = ImportStatus.FAILED
                record.error_message = message[:2000]
                record.finished_at = datetime.now(UTC)
                await session.commit()
        logger.error("import failed", extra={"import_id": str(import_id), "reason": message})

    async def _run(
        self,
        *,
        import_id: uuid.UUID,
        path: Path,
        sheet: str | None,
        default_date: date,
    ) -> ImportOutcome:
        table: SourceTable = await asyncio.to_thread(read_table, path, sheet=sheet)
        mapped = map_columns(list(table.headers), self._mapping)

        if not mapped.is_usable:
            message = (
                f"required columns not found: {list(mapped.missing_required)}; "
                f"file headers: {table.headers}"
            )
            await self._fail_import(import_id, message)
            return ImportOutcome(
                status=ImportStatus.FAILED,
                import_id=import_id,
                missing_required=mapped.missing_required,
                unmapped_headers=mapped.unmapped_headers,
                error_message=message,
            )

        if not table.rows:
            message = "file contains a header but no data rows"
            await self._fail_import(import_id, message)
            return ImportOutcome(
                status=ImportStatus.FAILED, import_id=import_id, error_message=message
            )

        rows = [
            normalize_row(row, table.headers, mapped, default_snapshot_date=default_date)
            for row in table.rows
        ]

        async with self._database.session() as session:
            outcome = await self._persist(
                session=session,
                import_id=import_id,
                rows=rows,
                mapped=mapped,
            )
            await session.commit()

        outcome.unmapped_headers = mapped.unmapped_headers
        return outcome

    async def _persist(
        self,
        *,
        session: AsyncSession,
        import_id: uuid.UUID,
        rows: list[NormalizedRow],
        mapped: MappedColumns,
    ) -> ImportOutcome:
        valid_rows = [row for row in rows if row.is_valid]

        niches = await NicheRepository(session).get_or_create_many(
            {row.category for row in valid_rows if row.category}
        )

        channel_repo = MarketChannelRepository(session)
        existing = await channel_repo.find_by_dedupe_keys(
            {row.dedupe_key for row in valid_rows if row.dedupe_key}
        )

        new_channels: dict[tuple[Platform, str], MarketChannel] = {}
        for row in valid_rows:
            if not row.dedupe_key:
                continue
            key = (row.platform, row.dedupe_key)
            channel = existing.get(key) or new_channels.get(key)
            if channel is None:
                channel = MarketChannel(
                    platform=row.platform,
                    dedupe_key=row.dedupe_key,
                    external_id=row.external_id,
                    channel_name=row.channel_name or row.dedupe_key,
                    channel_url=row.channel_url,
                    niche_id=niches[row.category].id if row.category else None,
                    first_seen_date=row.snapshot_date,
                    last_seen_date=row.snapshot_date,
                )
                new_channels[key] = channel
            else:
                self._refresh_channel(channel, row, niches)

        if new_channels:
            channel_repo.add_all(list(new_channels.values()))
            await session.flush()

        all_channels = {**existing, **new_channels}

        snapshots: list[dict] = []
        seen_snapshots: set[tuple[uuid.UUID, date]] = set()
        duplicates_in_file = 0

        row_payloads: list[dict] = []
        for row in rows:
            channel = None
            if row.is_valid and row.dedupe_key:
                channel = all_channels.get((row.platform, row.dedupe_key))

            if channel is not None and row.snapshot_date is not None:
                snapshot_key = (channel.id, row.snapshot_date)
                if snapshot_key in seen_snapshots:
                    duplicates_in_file += 1
                    row.warnings.append(
                        "duplicate channel+date within the file; snapshot from the "
                        "first occurrence kept"
                    )
                else:
                    seen_snapshots.add(snapshot_key)
                    snapshots.append(
                        {
                            "id": uuid.uuid4(),
                            "market_channel_id": channel.id,
                            "direct_import_id": import_id,
                            "snapshot_date": row.snapshot_date,
                            "subscribers": row.subscribers,
                            "err": row.err,
                            "predicted_views": row.predicted_views,
                            "cpv": row.cpv,
                            "campaign_price": row.campaign_price,
                            "currency": row.currency,
                            "region": row.region,
                            "extra": row.extra,
                        }
                    )

            row_payloads.append(
                {
                    "id": uuid.uuid4(),
                    "direct_import_id": import_id,
                    "row_number": row.row_number,
                    "status": RowStatus.VALID if row.is_valid else RowStatus.REJECTED,
                    "raw_data": row.raw,
                    "normalized_data": row.normalized,
                    "market_channel_id": channel.id if channel is not None else None,
                    "errors": row.errors,
                    "warnings": row.warnings,
                }
            )

        await DirectImportRepository(session).insert_rows(row_payloads)
        await MarketSnapshotRepository(session).insert_many(snapshots)

        rejected = sum(1 for row in rows if not row.is_valid)
        warning_count = sum(len(row.warnings) for row in rows)
        status = (
            ImportStatus.FAILED
            if not valid_rows
            else ImportStatus.PARTIAL
            if rejected
            else ImportStatus.SUCCESS
        )

        record = await session.get(DirectImport, import_id)
        assert record is not None
        record.status = status
        record.row_count = len(rows)
        record.valid_row_count = len(valid_rows)
        record.rejected_row_count = rejected
        record.warning_count = warning_count
        record.channels_created = len(new_channels)
        record.snapshots_created = len(snapshots)
        record.finished_at = datetime.now(UTC)
        record.column_mapping = {
            "mapped": dict(mapped.by_field),
            "unmapped_headers": list(mapped.unmapped_headers),
            "duplicate_headers": {k: list(v) for k, v in mapped.duplicate_headers.items()},
        }
        if status is ImportStatus.FAILED:
            record.error_message = "no valid rows in file"

        return ImportOutcome(
            status=status,
            import_id=import_id,
            row_count=len(rows),
            valid_row_count=len(valid_rows),
            rejected_row_count=rejected,
            warning_count=warning_count,
            channels_created=len(new_channels),
            snapshots_created=len(snapshots),
            duplicates_in_file=duplicates_in_file,
            warning_samples=[
                f"row {row.row_number}: {warning}"
                for row in rows
                for warning in row.warnings
            ][:5],
        )

    @staticmethod
    def _refresh_channel(channel: MarketChannel, row: NormalizedRow, niches: dict) -> None:
        """Keep the channel record current without discarding known values."""
        if row.snapshot_date:
            if channel.first_seen_date is None or row.snapshot_date < channel.first_seen_date:
                channel.first_seen_date = row.snapshot_date
            if channel.last_seen_date is None or row.snapshot_date > channel.last_seen_date:
                channel.last_seen_date = row.snapshot_date
        if row.channel_url and not channel.channel_url:
            channel.channel_url = row.channel_url
        if row.external_id and not channel.external_id:
            channel.external_id = row.external_id
        if row.category and channel.niche_id is None:
            channel.niche_id = niches[row.category].id
