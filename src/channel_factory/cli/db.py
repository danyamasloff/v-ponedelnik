"""Database inspection commands."""

from __future__ import annotations

import asyncio

import typer
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import func, select, text

from channel_factory.cli.app import app
from channel_factory.core.config import PROJECT_ROOT, get_settings, masked_database_url
from channel_factory.core.logging import setup_logging
from channel_factory.db.models import (
    DirectImport,
    DirectImportRow,
    MarketChannel,
    MarketSnapshot,
    Niche,
    NicheScore,
    NicheScoreRun,
    Source,
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
        raise typer.Exit(code=1) from exc


async def _report_status() -> None:
    database = Database(get_settings().database_url)
    try:
        async with database.session() as session:
            version = (await session.execute(text("SELECT version()"))).scalar_one()
            typer.echo(f"Server   : {str(version).split(' on ')[0]}")

            current = (
                await session.execute(text("SELECT version_num FROM alembic_version"))
            ).scalar_one_or_none()
            head = _head_revision()
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
