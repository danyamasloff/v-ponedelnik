"""Database inspection commands."""

from __future__ import annotations

import asyncio

import typer
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from channel_factory.cli.app import app
from channel_factory.core.config import (
    PROJECT_ROOT,
    diagnose_database_url,
    get_settings,
    masked_database_url,
)
from channel_factory.core.logging import setup_logging
from channel_factory.db.models import (
    AiGeneration,
    CompetitorWatch,
    DirectImport,
    DirectImportRow,
    MarketChannel,
    MarketSnapshot,
    Niche,
    NicheScore,
    NicheScoreRun,
    ResearchCluster,
    ResearchItem,
    ResearchSource,
    Source,
    TopicScore,
)
from channel_factory.db.session import Database

COUNTED_MODELS = (
    Source,
    Niche,
    DirectImport,
    DirectImportRow,
    MarketChannel,
    MarketSnapshot,
    NicheScoreRun,
    NicheScore,
    CompetitorWatch,
    ResearchSource,
    ResearchItem,
    ResearchCluster,
    TopicScore,
    AiGeneration,
)


def _head_revision() -> str | None:
    """Latest revision defined in the alembic/ directory."""
    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    return ScriptDirectory.from_config(config).get_current_head()


@app.command("db-status")
def db_status() -> None:
    """Check the database connection, migration state and table counts."""
    settings = get_settings()
    setup_logging(settings.log_level)
    typer.echo(f"Database : {masked_database_url(settings.database_url)}")

    try:
        asyncio.run(_report_status())
    except Exception as exc:
        typer.secho(f"Status   : UNREACHABLE ({type(exc).__name__}: {exc})", fg=typer.colors.RED)
        # The driver error names the symptom; these name the fix.
        for problem in diagnose_database_url(settings.database_url):
            typer.secho(f"  ! {problem}", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1) from exc


async def _has_table(session: AsyncSession, name: str) -> bool:
    """Whether a table exists, asked of the catalogue rather than by querying it.

    Selecting from a missing table raises, and that exception would surface as
    a connection failure two frames up.
    """
    found = await session.execute(text("SELECT to_regclass(:name)"), {"name": name})
    return found.scalar_one_or_none() is not None


async def _report_status() -> None:
    database = Database(get_settings().database_url)
    try:
        async with database.session() as session:
            version = (await session.execute(text("SELECT version()"))).scalar_one()
            typer.echo(f"Server   : {str(version).split(' on ')[0]}")

            # A brand-new database has no alembic_version table at all. That
            # is not a connection problem and must not be reported as one:
            # confusing "cannot reach the database" with "schema not applied
            # yet" sends whoever set it up looking for the wrong fault.
            head = _head_revision()
            if not await _has_table(session, "alembic_version"):
                typer.secho(
                    f"Migration: не применены (пустая база, head {head})",
                    fg=typer.colors.YELLOW,
                )
                typer.echo("Tables   : схемы ещё нет")
                typer.secho(
                    "Status   : ПОДКЛЮЧЕНИЕ ЕСТЬ, нужна миграция "
                    "(alembic upgrade head)",
                    fg=typer.colors.YELLOW,
                )
                return

            current = (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
            if current is None:
                state = "no migrations applied"
            elif current == head:
                state = "up to date"
            else:
                state = f"BEHIND head {head}"
            typer.echo(f"Migration: {current or '-'} ({state})")

            counts = []
            for model in COUNTED_MODELS:
                total = (
                    await session.execute(select(func.count()).select_from(model))
                ).scalar_one()
                counts.append(f"{model.__tablename__}={total}")
            typer.echo("Tables   : " + "  ".join(counts))
        typer.secho("Status   : OK", fg=typer.colors.GREEN)
    finally:
        await database.dispose()
