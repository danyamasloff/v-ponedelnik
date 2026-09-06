"""Integration tests for competitor research against PostgreSQL."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import func, select

from channel_factory.competitors.analysis import LeaderMetric
from channel_factory.competitors.reports import report_filename, write_competitor_report
from channel_factory.competitors.service import CompetitorService
from channel_factory.core.enums import Platform
from channel_factory.db.models import CompetitorWatch, MarketChannel
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

NICHE = "Технологии"
OTHER = "Карьера"

# Subscribers deliberately span two orders of magnitude, and "TG Tiny Hype" is a
# small channel with an extreme ERR — the case the engagement floor exists for.
CHANNELS = [
    ("TG Tech A", "https://t.me/techa", NICHE, 100_000, "12,0%", 20_000, "1,50", 90_000),
    ("TG Tech B", "https://t.me/techb", NICHE, 60_000, "14,0%", 12_000, "1,40", 60_000),
    ("TG Tech C", "https://t.me/techc", NICHE, 30_000, "16,0%", 6_000, "1,30", 35_000),
    ("TG Tech D", "https://t.me/techd", NICHE, 20_000, "18,0%", 4_000, "1,20", 24_000),
    ("TG Tech E", "https://t.me/teche", NICHE, 10_000, "9,0%", 1_500, "1,10", 12_000),
    ("TG Tiny Hype", "https://t.me/tinyhype", NICHE, 2_000, "95,0%", 1_900, "0,90", 2_000),
    ("MAX Tech A", "https://max.ru/mtecha", NICHE, 15_000, "20,0%", 3_500, "0,80", 14_000),
    ("MAX Tech B", "https://max.ru/mtechb", NICHE, 9_000, "22,0%", 2_200, "0,70", 9_000),
    ("MAX Tech C", "https://max.ru/mtechc", NICHE, 5_000, "24,0%", 1_400, "0,60", 5_000),
    ("TG Career A", "https://t.me/cara", OTHER, 40_000, "10,0%", 5_000, "0,90", 30_000),
    ("TG Career B", "https://t.me/carb", OTHER, 12_000, "11,0%", 1_600, "0,85", 11_000),
    ("TG Career C", "https://t.me/carc", OTHER, 6_000, "13,0%", 900, "0,80", 6_000),
]

TECH_SLUG = "tehnologii"


def rows(snapshot_date: str = "2026-09-01") -> list[list[object]]:
    return [
        [name, url, category, str(subs), err, str(views), cpv, str(price), snapshot_date]
        for name, url, category, subs, err, views, cpv, price in CHANNELS
    ]


@pytest.fixture
async def imported(database, column_mapping, xlsx_factory):
    await DirectImportService(database, column_mapping).import_file(
        xlsx_factory(HEADERS, rows(), name="market.xlsx")
    )
    return CompetitorService(database)


class TestBenchmarks:
    async def test_describes_the_niche_distribution(self, imported) -> None:
        dossier = await imported.niche_dossier(TECH_SLUG)
        benchmarks = dossier.benchmarks

        assert benchmarks.channels_count == 9
        assert benchmarks.subscribers_max == 100_000
        assert benchmarks.subscribers_p50 == 15_000
        assert benchmarks.subscribers_p25 < benchmarks.subscribers_p50
        assert benchmarks.subscribers_p90 > benchmarks.subscribers_p75
        assert benchmarks.head_share is not None
        assert 0 < float(benchmarks.head_share) <= 1

    async def test_scopes_to_one_platform(self, imported) -> None:
        telegram = await imported.niche_dossier(TECH_SLUG, platform=Platform.TELEGRAM)
        maximum = await imported.niche_dossier(TECH_SLUG, platform=Platform.MAX)

        assert telegram.benchmarks.channels_count == 6
        assert maximum.benchmarks.channels_count == 3
        assert maximum.benchmarks.subscribers_max == 15_000
        assert all(p.platform == "MAX" for p in maximum.by_subscribers)

    async def test_unknown_niche_returns_none(self, imported) -> None:
        assert await imported.niche_dossier("no-such-niche") is None

    async def test_niche_without_channels_on_a_platform(self, imported) -> None:
        assert await imported.niche_dossier("karera", platform=Platform.MAX) is None


class TestLeaders:
    async def test_ranked_by_subscribers(self, imported) -> None:
        dossier = await imported.niche_dossier(TECH_SLUG, limit=3)
        assert [p.channel_name for p in dossier.by_subscribers] == [
            "TG Tech A",
            "TG Tech B",
            "TG Tech C",
        ]

    async def test_engagement_floor_excludes_small_high_err_channels(self, imported) -> None:
        """A 2k-subscriber channel with 95% ERR must not head the engagement board."""
        dossier = await imported.niche_dossier(TECH_SLUG)

        assert dossier.engagement_floor == 15_000
        names = [p.channel_name for p in dossier.by_engagement]
        assert "TG Tiny Hype" not in names
        assert names[0] == "MAX Tech A", "highest ERR among channels at or above the median"

    async def test_position_is_computed_within_niche_and_platform(self, imported) -> None:
        dossier = await imported.niche_dossier(TECH_SLUG)
        by_name = {p.channel_name: p for p in dossier.by_subscribers}

        # Largest Telegram channel and largest MAX channel are both at the top of
        # their own platform, despite very different absolute sizes.
        assert by_name["TG Tech A"].subscribers_pct == pytest.approx(100.0)
        max_leaders = await imported.niche_dossier(TECH_SLUG, platform=Platform.MAX)
        assert max_leaders.by_subscribers[0].channel_name == "MAX Tech A"
        assert max_leaders.by_subscribers[0].subscribers_pct == pytest.approx(100.0)

    async def test_price_board(self, imported) -> None:
        dossier = await imported.niche_dossier(TECH_SLUG, limit=2)
        assert [p.channel_name for p in dossier.by_price] == ["TG Tech A", "TG Tech B"]

    async def test_leaders_of_another_niche_are_not_mixed_in(self, imported) -> None:
        dossier = await imported.niche_dossier(TECH_SLUG)
        assert all(p.niche_slug == TECH_SLUG for p in dossier.by_subscribers)


class TestSearchAndWatchlist:
    async def test_search_by_name_and_url(self, imported) -> None:
        by_name = await imported.search("Tiny")
        by_url = await imported.search("max.ru/mtecha")

        assert [p.channel_name for p in by_name] == ["TG Tiny Hype"]
        assert [p.channel_name for p in by_url] == ["MAX Tech A"]

    async def test_watch_and_unwatch(self, imported, database) -> None:
        found = await imported.search("TG Tech A")
        channel_id = found[0].channel_id

        watched = await imported.watch(channel_id, notes="эталон ниши")
        assert watched is not None and watched.channel_name == "TG Tech A"

        listed = await imported.watchlist()
        assert [p.channel_name for p in listed] == ["TG Tech A"]
        assert listed[0].watch_notes == "эталон ниши"

        assert await imported.unwatch(channel_id) is True
        assert await imported.watchlist() == []
        async with database.session() as session:
            remaining = (
                await session.execute(select(func.count()).select_from(CompetitorWatch))
            ).scalar_one()
        assert remaining == 0

    async def test_watching_twice_updates_the_note(self, imported, database) -> None:
        channel_id = (await imported.search("TG Tech B"))[0].channel_id
        await imported.watch(channel_id, notes="первая заметка")
        await imported.watch(channel_id, notes="уточнённая заметка")

        listed = await imported.watchlist()
        assert len(listed) == 1
        assert listed[0].watch_notes == "уточнённая заметка"

    async def test_unwatch_missing_channel_is_false(self, imported, database) -> None:
        async with database.session() as session:
            channel = (await session.execute(select(MarketChannel).limit(1))).scalar_one()
        assert await imported.unwatch(channel.id) is False

    async def test_watch_unknown_channel_returns_none(self, imported) -> None:
        import uuid

        assert await imported.watch(uuid.uuid4()) is None

    async def test_watched_channels_appear_in_the_dossier(self, imported) -> None:
        channel_id = (await imported.search("TG Tech C"))[0].channel_id
        await imported.watch(channel_id, notes="следим")

        dossier = await imported.niche_dossier(TECH_SLUG)
        assert [p.channel_name for p in dossier.watched] == ["TG Tech C"]


class TestReport:
    async def test_report_is_written_with_scope_in_the_name(
        self, imported, tmp_path: Path
    ) -> None:
        overall = await imported.niche_dossier(TECH_SLUG)
        telegram = await imported.niche_dossier(TECH_SLUG, platform=Platform.TELEGRAM)

        overall_path = write_competitor_report(tmp_path, overall)
        telegram_path = write_competitor_report(tmp_path, telegram)

        assert overall_path.name == "competitor-analysis-tehnologii.md"
        assert telegram_path.name == "competitor-analysis-tehnologii-telegram.md"
        assert overall_path != telegram_path

    async def test_report_states_leaders_thresholds_and_limits(
        self, imported, tmp_path: Path
    ) -> None:
        dossier = await imported.niche_dossier(TECH_SLUG)
        content = write_competitor_report(tmp_path, dossier).read_text(encoding="utf-8")

        assert "TG Tech A" in content
        assert "Что нужно, чтобы конкурировать" in content
        assert "не отвечает на вопрос «какой контент работает»" in content
        assert "Один снимок во времени" in content
        assert "медиана ниши" in content, "the engagement floor must be explained"

    def test_filename_helper(self) -> None:
        from channel_factory.competitors.analysis import NicheBenchmarks

        overall = NicheBenchmarks(slug="ai", name="AI", platform=None, channels_count=1)
        scoped = NicheBenchmarks(
            slug="ai", name="AI", platform=Platform.MAX, channels_count=1
        )
        assert report_filename(overall) == "competitor-analysis-ai.md"
        assert report_filename(scoped) == "competitor-analysis-ai-max.md"


class TestLeaderMetricEnum:
    async def test_every_metric_is_queryable(self, imported, database) -> None:
        """Guards the whitelist that maps a metric to an SQL column."""
        from channel_factory.competitors.analysis import CompetitorRepository

        async with database.session() as session:
            repo = CompetitorRepository(session)
            for metric in LeaderMetric:
                leaders = await repo.niche_leaders(TECH_SLUG, metric=metric, limit=2)
                assert leaders, f"no leaders returned for {metric}"
