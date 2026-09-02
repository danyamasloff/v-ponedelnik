"""Integration tests: the full import pipeline against a real PostgreSQL database."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from channel_factory.core.enums import ImportStatus, Platform, RowStatus
from channel_factory.db.models import (
    DirectImport,
    DirectImportRow,
    MarketChannel,
    MarketSnapshot,
    Niche,
)
from channel_factory.direct.importer.pipeline import DirectImportService

pytestmark = pytest.mark.integration

HEADERS = [
    "Канал",
    "Ссылка",
    "Категория",
    "Подписчики",
    "ERR",
    "Прогноз просмотров",
    "CPV",
    "Стоимость размещения",
    "Дата",
]

ROWS = [
    [
        "AI Tools",
        "https://t.me/aitools",
        "AI и технологии",
        "12 500",
        "24,7%",
        "3 200",
        "1,25",
        "15 000 ₽",
        "2026-09-01",
    ],
    [
        "Neuro News",
        "https://t.me/neuronews",
        "AI и технологии",
        "8 000",
        "18,2%",
        "1 900",
        "0,95",
        "9 500 ₽",
        "2026-09-01",
    ],
    [
        "Career IT",
        "https://max.ru/careerit",
        "Карьера",
        "5 000",
        "12,0%",
        "900",
        "1,10",
        "6 000 ₽",
        "2026-09-01",
    ],
]


async def _count(database, model) -> int:
    async with database.session() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


class TestSuccessfulImport:
    async def test_writes_channels_snapshots_and_rows(
        self, database, column_mapping, xlsx_factory
    ) -> None:
        service = DirectImportService(database, column_mapping)
        outcome = await service.import_file(xlsx_factory(HEADERS, ROWS))

        assert outcome.status is ImportStatus.SUCCESS
        assert outcome.row_count == 3
        assert outcome.valid_row_count == 3
        assert outcome.rejected_row_count == 0
        assert outcome.channels_created == 3
        assert outcome.snapshots_created == 3

        assert await _count(database, MarketChannel) == 3
        assert await _count(database, MarketSnapshot) == 3
        assert await _count(database, DirectImportRow) == 3
        assert await _count(database, Niche) == 2

    async def test_normalizes_values_and_detects_platforms(
        self, database, column_mapping, xlsx_factory
    ) -> None:
        service = DirectImportService(database, column_mapping)
        await service.import_file(xlsx_factory(HEADERS, ROWS))

        async with database.session() as session:
            channel = (
                await session.execute(
                    select(MarketChannel).where(MarketChannel.channel_name == "AI Tools")
                )
            ).scalar_one()
            snapshot = (
                await session.execute(
                    select(MarketSnapshot).where(
                        MarketSnapshot.market_channel_id == channel.id
                    )
                )
            ).scalar_one()

            assert channel.platform is Platform.TELEGRAM
            assert channel.channel_url == "https://t.me/aitools"
            assert channel.dedupe_key == "url:t.me/aitools"
            assert snapshot.subscribers == 12500
            assert snapshot.err == Decimal("0.247000")
            assert snapshot.predicted_views == 3200
            assert snapshot.cpv == Decimal("1.250000")
            assert snapshot.campaign_price == Decimal("15000.00")
            assert snapshot.currency == "RUB"
            assert snapshot.snapshot_date == date(2026, 9, 1)

            max_channel = (
                await session.execute(
                    select(MarketChannel).where(MarketChannel.channel_name == "Career IT")
                )
            ).scalar_one()
            assert max_channel.platform is Platform.MAX

    async def test_preserves_raw_values(self, database, column_mapping, xlsx_factory) -> None:
        service = DirectImportService(database, column_mapping)
        await service.import_file(xlsx_factory(HEADERS, ROWS))

        async with database.session() as session:
            row = (
                await session.execute(
                    select(DirectImportRow).where(DirectImportRow.row_number == 2)
                )
            ).scalar_one()
            assert row.raw_data["ERR"] == "24,7%"
            assert row.normalized_data["err"] == "0.247"

    async def test_reads_csv_as_well(self, database, column_mapping, csv_factory) -> None:
        service = DirectImportService(database, column_mapping)
        outcome = await service.import_file(csv_factory(HEADERS, ROWS))
        assert outcome.status is ImportStatus.SUCCESS
        assert outcome.snapshots_created == 3


class TestIdempotency:
    async def test_reimporting_the_same_file_is_skipped(
        self, database, column_mapping, xlsx_factory
    ) -> None:
        service = DirectImportService(database, column_mapping)
        path = xlsx_factory(HEADERS, ROWS)
        first = await service.import_file(path)

        second = await service.import_file(path)

        assert second.skipped is True
        assert second.duplicate_of == first.import_id
        assert await _count(database, DirectImport) == 1
        assert await _count(database, MarketSnapshot) == 3

    async def test_force_creates_a_second_import_run(
        self, database, column_mapping, xlsx_factory
    ) -> None:
        service = DirectImportService(database, column_mapping)
        path = xlsx_factory(HEADERS, ROWS)
        await service.import_file(path)

        forced = await service.import_file(path, force=True)

        assert forced.skipped is False
        assert forced.status is ImportStatus.SUCCESS
        assert forced.channels_created == 0, "existing channels must be reused, not duplicated"
        assert forced.snapshots_created == 3
        assert await _count(database, DirectImport) == 2
        assert await _count(database, MarketChannel) == 3
        assert await _count(database, MarketSnapshot) == 6

    async def test_a_different_file_is_not_skipped(
        self, database, column_mapping, xlsx_factory
    ) -> None:
        service = DirectImportService(database, column_mapping)
        await service.import_file(xlsx_factory(HEADERS, ROWS, name="first.xlsx"))
        outcome = await service.import_file(
            xlsx_factory(HEADERS, ROWS[:1], name="second.xlsx")
        )
        assert outcome.skipped is False
        assert await _count(database, DirectImport) == 2


class TestHistoricalData:
    async def test_same_channel_in_two_exports_gets_one_row_and_two_snapshots(
        self, database, column_mapping, xlsx_factory
    ) -> None:
        service = DirectImportService(database, column_mapping)
        september = [list(ROWS[0])]
        october = [list(ROWS[0])]
        october[0][3] = "18 900"
        october[0][8] = "2026-10-01"

        await service.import_file(xlsx_factory(HEADERS, september, name="sep.xlsx"))
        await service.import_file(xlsx_factory(HEADERS, october, name="oct.xlsx"))

        assert await _count(database, MarketChannel) == 1
        assert await _count(database, MarketSnapshot) == 2

        async with database.session() as session:
            channel = (await session.execute(select(MarketChannel))).scalar_one()
            snapshots = (
                (
                    await session.execute(
                        select(MarketSnapshot).order_by(MarketSnapshot.snapshot_date)
                    )
                )
                .scalars()
                .all()
            )
            assert [s.subscribers for s in snapshots] == [12500, 18900]
            assert channel.first_seen_date == date(2026, 9, 1)
            assert channel.last_seen_date == date(2026, 10, 1)


class TestErrorHandling:
    async def test_one_bad_row_does_not_fail_the_import(
        self, database, column_mapping, xlsx_factory
    ) -> None:
        rows = [*ROWS, ["", "https://t.me/anon", "AI и технологии", "1 000", "", "", "", "", ""]]
        service = DirectImportService(database, column_mapping)

        outcome = await service.import_file(xlsx_factory(HEADERS, rows))

        assert outcome.status is ImportStatus.PARTIAL
        assert outcome.valid_row_count == 3
        assert outcome.rejected_row_count == 1
        assert await _count(database, MarketSnapshot) == 3

        async with database.session() as session:
            rejected = (
                await session.execute(
                    select(DirectImportRow).where(DirectImportRow.status == RowStatus.REJECTED)
                )
            ).scalar_one()
            assert "channel_name" in rejected.errors[0]
            assert rejected.raw_data, "rejected rows must keep their raw values"

    async def test_unparseable_metric_becomes_a_warning_not_a_rejection(
        self, database, column_mapping, xlsx_factory
    ) -> None:
        rows = [list(ROWS[0])]
        rows[0][3] = "много"
        service = DirectImportService(database, column_mapping)

        outcome = await service.import_file(xlsx_factory(HEADERS, rows))

        assert outcome.status is ImportStatus.SUCCESS
        assert outcome.warning_count >= 1
        async with database.session() as session:
            snapshot = (await session.execute(select(MarketSnapshot))).scalar_one()
            row = (await session.execute(select(DirectImportRow))).scalar_one()
            assert snapshot.subscribers is None
            assert any("subscribers" in warning for warning in row.warnings)
            assert row.raw_data["Подписчики"] == "много"

    async def test_missing_required_column_fails_the_whole_import(
        self, database, column_mapping, xlsx_factory
    ) -> None:
        headers = [header for header in HEADERS if header != "Канал"]
        rows = [row[1:] for row in ROWS]
        service = DirectImportService(database, column_mapping)

        outcome = await service.import_file(xlsx_factory(headers, rows))

        assert outcome.status is ImportStatus.FAILED
        assert outcome.missing_required == ("channel_name",)
        assert await _count(database, MarketSnapshot) == 0
        async with database.session() as session:
            record = (await session.execute(select(DirectImport))).scalar_one()
            assert record.status is ImportStatus.FAILED
            assert "channel_name" in (record.error_message or "")

    async def test_failed_import_is_not_treated_as_already_imported(
        self, database, column_mapping, xlsx_factory
    ) -> None:
        service = DirectImportService(database, column_mapping)
        path = xlsx_factory([header for header in HEADERS if header != "Канал"], [])
        await service.import_file(path)

        second = await service.import_file(path)
        assert second.skipped is False

    async def test_duplicate_channel_within_one_file_keeps_first_occurrence(
        self, database, column_mapping, xlsx_factory
    ) -> None:
        service = DirectImportService(database, column_mapping)
        outcome = await service.import_file(xlsx_factory(HEADERS, [ROWS[0], ROWS[0]]))

        assert outcome.duplicates_in_file == 1
        assert outcome.snapshots_created == 1
        assert await _count(database, MarketChannel) == 1
