"""Market-data import commands."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date
from pathlib import Path

import typer

from channel_factory.cli.app import app
from channel_factory.core.config import get_settings
from channel_factory.core.enums import ImportStatus, RowStatus
from channel_factory.core.logging import setup_logging
from channel_factory.db.repositories.imports import DirectImportRepository
from channel_factory.db.session import Database
from channel_factory.direct.importer.pipeline import (
    DEFAULT_SOURCE_NAME,
    DirectImportService,
    ImportOutcome,
)
from channel_factory.direct.mapping.loader import (
    ColumnMappingConfig,
    ColumnMappingError,
    load_column_mapping,
)

STATUS_COLORS = {
    ImportStatus.SUCCESS: typer.colors.GREEN,
    ImportStatus.PARTIAL: typer.colors.YELLOW,
    ImportStatus.FAILED: typer.colors.RED,
    ImportStatus.RUNNING: typer.colors.BLUE,
}


@app.command("import-direct")
def import_direct(
    path: Path = typer.Argument(..., exists=True, dir_okay=False, help="XLSX/CSV export file"),
    force: bool = typer.Option(
        False, "--force", help="Import again even if this exact file was already imported"
    ),
    source: str = typer.Option(DEFAULT_SOURCE_NAME, "--source", help="Data source label"),
    snapshot_date: str | None = typer.Option(
        None,
        "--snapshot-date",
        help="YYYY-MM-DD used for rows without a date column (default: today, UTC)",
    ),
    sheet: str | None = typer.Option(None, "--sheet", help="XLSX sheet name (default: first)"),
) -> None:
    """Import a Yandex Direct / catalog export into PostgreSQL."""
    settings = get_settings()
    setup_logging(settings.log_level)

    parsed_date: date | None = None
    if snapshot_date:
        try:
            parsed_date = date.fromisoformat(snapshot_date)
        except ValueError as exc:
            typer.secho(f"Invalid --snapshot-date: {snapshot_date}", fg=typer.colors.RED)
            raise typer.Exit(code=2) from exc

    try:
        mapping = load_column_mapping(settings.direct_columns_config)
    except ColumnMappingError as exc:
        typer.secho(f"Column mapping error: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc

    outcome = asyncio.run(
        _run_import(
            path=path,
            force=force,
            source=source,
            snapshot_date=parsed_date,
            sheet=sheet,
            mapping=mapping,
        )
    )
    _print_outcome(path, outcome)
    if outcome.status is ImportStatus.FAILED and not outcome.skipped:
        raise typer.Exit(code=1)


async def _run_import(
    *,
    path: Path,
    force: bool,
    source: str,
    snapshot_date: date | None,
    sheet: str | None,
    mapping: ColumnMappingConfig,
) -> ImportOutcome:
    database = Database(get_settings().database_url)
    try:
        service = DirectImportService(database, mapping)
        return await service.import_file(
            path, force=force, source_name=source, snapshot_date=snapshot_date, sheet=sheet
        )
    finally:
        await database.dispose()


def _print_outcome(path: Path, outcome: ImportOutcome) -> None:
    if outcome.skipped:
        typer.secho(
            f"SKIPPED  : {path.name} was already imported "
            f"at {outcome.duplicate_imported_at:%Y-%m-%d %H:%M:%S} UTC",
            fg=typer.colors.YELLOW,
        )
        typer.echo(f"Import id: {outcome.duplicate_of}")
        typer.echo("Use --force to import the same file again as a new run.")
        return

    typer.secho(f"Status   : {outcome.status.value}", fg=STATUS_COLORS[outcome.status])
    typer.echo(f"Import id: {outcome.import_id}")
    if outcome.error_message:
        typer.secho(f"Error    : {outcome.error_message}", fg=typer.colors.RED)
        return

    typer.echo(
        f"Rows     : {outcome.row_count} total, {outcome.valid_row_count} valid, "
        f"{outcome.rejected_row_count} rejected, {outcome.warning_count} warnings"
    )
    typer.echo(
        f"Written  : {outcome.channels_created} new channels, "
        f"{outcome.snapshots_created} snapshots"
    )
    if outcome.duplicates_in_file:
        typer.echo(f"Duplicates in file: {outcome.duplicates_in_file} (first occurrence kept)")
    if outcome.unmapped_headers:
        typer.echo(f"Unmapped columns  : {', '.join(outcome.unmapped_headers)}")
    for sample in outcome.warning_samples:
        typer.echo(f"  warning: {sample}")
    typer.echo(f"Duration : {outcome.duration_ms} ms")


@app.command("list-imports")
def list_imports(
    limit: int = typer.Option(20, "--limit", min=1, max=200, help="How many runs to show"),
) -> None:
    """Show recent import runs."""
    settings = get_settings()
    setup_logging(settings.log_level)
    asyncio.run(_list_imports(limit))


async def _list_imports(limit: int) -> None:
    database = Database(get_settings().database_url)
    try:
        async with database.session() as session:
            imports = await DirectImportRepository(session).list_recent(limit)
        if not imports:
            typer.echo("No imports yet.")
            return
        header = (
            f"{'STARTED (UTC)':19}  {'STATUS':8}  {'ROWS':>6}  {'VALID':>6}  "
            f"{'REJ':>4}  {'WARN':>5}  {'ID':36}  FILE"
        )
        typer.echo(header)
        typer.echo("-" * len(header))
        for record in imports:
            typer.echo(
                f"{record.created_at:%Y-%m-%d %H:%M:%S}  {record.status.value:8}  "
                f"{record.row_count:>6}  {record.valid_row_count:>6}  "
                f"{record.rejected_row_count:>4}  {record.warning_count:>5}  "
                f"{record.id}  {record.original_filename}"
            )
    finally:
        await database.dispose()


@app.command("show-import")
def show_import(
    import_id: str = typer.Argument(..., help="Import UUID (see list-imports)"),
    rejected_limit: int = typer.Option(10, "--rejected", min=0, max=100),
    warning_limit: int = typer.Option(10, "--warnings", min=0, max=100),
) -> None:
    """Show details of one import run, including rejected rows and warnings."""
    settings = get_settings()
    setup_logging(settings.log_level)
    try:
        parsed_id = uuid.UUID(import_id)
    except ValueError as exc:
        typer.secho(f"Not a valid UUID: {import_id}", fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc
    asyncio.run(_show_import(parsed_id, rejected_limit, warning_limit))


async def _show_import(import_id: uuid.UUID, rejected_limit: int, warning_limit: int) -> None:
    database = Database(get_settings().database_url)
    try:
        async with database.session() as session:
            repo = DirectImportRepository(session)
            record = await repo.get(import_id)
            if record is None:
                typer.secho(f"Import {import_id} not found.", fg=typer.colors.RED)
                raise typer.Exit(code=1)

            typer.echo(f"File      : {record.original_filename}")
            typer.echo(f"Checksum  : {record.file_checksum}")
            typer.echo(f"Size      : {record.file_size} bytes")
            typer.secho(
                f"Status    : {record.status.value}", fg=STATUS_COLORS[record.status]
            )
            typer.echo(f"Started   : {record.created_at:%Y-%m-%d %H:%M:%S} UTC")
            if record.finished_at:
                typer.echo(f"Finished  : {record.finished_at:%Y-%m-%d %H:%M:%S} UTC")
            typer.echo(f"Forced    : {record.forced}")
            typer.echo(
                f"Rows      : {record.row_count} total, {record.valid_row_count} valid, "
                f"{record.rejected_row_count} rejected, {record.warning_count} warnings"
            )
            typer.echo(
                f"Written   : {record.channels_created} channels, "
                f"{record.snapshots_created} snapshots"
            )
            if record.error_message:
                typer.secho(f"Error     : {record.error_message}", fg=typer.colors.RED)

            mapping = record.column_mapping or {}
            if mapping.get("mapped"):
                typer.echo(f"Mapped    : {', '.join(sorted(mapping['mapped']))}")
            if mapping.get("unmapped_headers"):
                typer.echo(f"Unmapped  : {', '.join(mapping['unmapped_headers'])}")

            if rejected_limit:
                rejected = await repo.sample_rows(
                    import_id, status=RowStatus.REJECTED, limit=rejected_limit
                )
                if rejected:
                    typer.echo(f"\nRejected rows (first {len(rejected)}):")
                    for row in rejected:
                        typer.echo(f"  row {row.row_number}: {'; '.join(row.errors)}")

            if warning_limit:
                warned = await repo.sample_rows(
                    import_id, with_warnings=True, limit=warning_limit
                )
                if warned:
                    typer.echo(f"\nRows with warnings (first {len(warned)}):")
                    for row in warned:
                        typer.echo(f"  row {row.row_number}: {'; '.join(row.warnings)}")
    finally:
        await database.dispose()
