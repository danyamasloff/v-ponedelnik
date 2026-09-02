"""Shared test fixtures.

Integration tests run against a dedicated ``*_test`` database that is created
and migrated once per session. The name is asserted so a misconfigured
TEST_DATABASE_URL can never truncate real data.
"""

from __future__ import annotations

import asyncio
import csv as csv_module
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from openpyxl import Workbook
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from channel_factory.core.config import PROJECT_ROOT, get_settings
from channel_factory.db.session import Database
from channel_factory.direct.mapping.loader import load_column_mapping
from channel_factory.niches.config import load_niche_score_config

TRUNCATED_TABLES = (
    "direct_import_rows",
    "market_snapshots",
    "niche_scores",
    "niche_score_runs",
    "direct_imports",
    "market_channels",
    "niches",
    "sources",
)


async def _ensure_database(url: str) -> None:
    """Create the test database if it does not exist yet."""
    target = make_url(url)
    admin_url = target.set(database="postgres")
    engine = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            exists = (
                await connection.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :name"),
                    {"name": target.database},
                )
            ).scalar_one_or_none()
            if not exists:
                await connection.execute(text(f'CREATE DATABASE "{target.database}"'))
    finally:
        await engine.dispose()


@pytest.fixture(scope="session")
def test_database_url() -> str:
    """URL of the migrated test database.

    This fixture is synchronous on purpose: Alembic's env.py calls
    ``asyncio.run`` itself, which cannot be nested inside a running loop.
    """
    url = get_settings().effective_test_database_url
    database_name = make_url(url).database or ""
    if not database_name.endswith("_test"):
        raise RuntimeError(
            f"refusing to run tests against {database_name!r}: "
            "the test database name must end with '_test'"
        )

    asyncio.run(_ensure_database(url))

    config = Config(str(PROJECT_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(PROJECT_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", url)
    command.upgrade(config, "head")
    return url


@pytest.fixture
async def database(test_database_url: str):
    """A clean database handle for one test."""
    db = Database(test_database_url)
    async with db.session() as session:
        await session.execute(
            text(f"TRUNCATE TABLE {', '.join(TRUNCATED_TABLES)} RESTART IDENTITY CASCADE")
        )
        await session.commit()
    try:
        yield db
    finally:
        await db.dispose()


@pytest.fixture(scope="session")
def column_mapping():
    """The project's real column mapping configuration."""
    return load_column_mapping(get_settings().direct_columns_config)


@pytest.fixture(scope="session")
def niche_score_config():
    """The project's real niche score configuration."""
    return load_niche_score_config(get_settings().niche_score_config)


@pytest.fixture
def xlsx_factory(tmp_path: Path) -> Callable[..., Path]:
    """Build a small XLSX file for tests."""

    def build(
        headers: Sequence[Any],
        rows: Sequence[Sequence[Any]],
        *,
        name: str = "export.xlsx",
        title_rows: Sequence[Sequence[Any]] = (),
    ) -> Path:
        workbook = Workbook()
        sheet = workbook.active
        for title_row in title_rows:
            sheet.append(list(title_row))
        sheet.append(list(headers))
        for row in rows:
            sheet.append(list(row))
        path = tmp_path / name
        workbook.save(path)
        return path

    return build


@pytest.fixture
def csv_factory(tmp_path: Path) -> Callable[..., Path]:
    """Build a small CSV file for tests."""

    def build(
        headers: Sequence[Any],
        rows: Sequence[Sequence[Any]],
        *,
        name: str = "export.csv",
        delimiter: str = ";",
        encoding: str = "utf-8",
    ) -> Path:
        path = tmp_path / name
        with path.open("w", encoding=encoding, newline="") as handle:
            writer = csv_module.writer(handle, delimiter=delimiter)
            writer.writerow(list(headers))
            writer.writerows([list(row) for row in rows])
        return path

    return build
