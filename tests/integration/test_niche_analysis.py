"""Integration tests: market statistics, scoring and reports against PostgreSQL."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import func, select

from channel_factory.core.enums import Platform
from channel_factory.db.models import NicheScore, NicheScoreRun
from channel_factory.db.repositories.niches import NicheScoreRepository
from channel_factory.direct.importer.pipeline import DirectImportService
from channel_factory.niches.cross_platform import compare_platforms
from channel_factory.niches.reports import (
    MARKET_OVERVIEW_FILE,
    NICHE_RANKING_CSV,
    NICHE_RANKING_FILE,
    build_rows,
    write_reports,
)
from channel_factory.niches.service import NicheAnalysisService

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

# Three channels per niche so both clear the min_channels_for_scoring threshold.
AI = "AI и технологии"
CAREER = "Карьера"
CHANNELS = [
    ("AI Tools", "https://t.me/aitools", AI, 10_000, "25,0%", 3_000, "1,50", 20_000),
    ("Neuro News", "https://t.me/neuronews", AI, 20_000, "22,0%", 5_000, "1,40", 30_000),
    ("Prompt Lab", "https://t.me/promptlab", AI, 30_000, "20,0%", 7_000, "1,30", 40_000),
    ("Career IT", "https://t.me/careerit", CAREER, 5_000, "12,0%", 900, "0,60", 6_000),
    ("Job Hunt", "https://t.me/jobhunt", CAREER, 6_000, "11,0%", 1_000, "0,55", 7_000),
    ("Dev Path", "https://t.me/devpath", CAREER, 7_000, "10,0%", 1_100, "0,50", 8_000),
]


def build_rows_for(snapshot_date: str, *, subscriber_factor: float = 1.0) -> list[list[object]]:
    return [
        [
            name,
            url,
            category,
            str(int(subscribers * subscriber_factor)),
            err,
            str(views),
            cpv,
            str(price),
            snapshot_date,
        ]
        for name, url, category, subscribers, err, views, cpv, price in CHANNELS
    ]


async def import_export(
    database, column_mapping, xlsx_factory, snapshot_date: str, *, factor: float = 1.0, name: str
):
    service = DirectImportService(database, column_mapping)
    path = xlsx_factory(HEADERS, build_rows_for(snapshot_date, subscriber_factor=factor), name=name)
    return await service.import_file(path)


class TestNoData:
    async def test_empty_database_reports_no_data(self, database, niche_score_config) -> None:
        outcome = await NicheAnalysisService(database, niche_score_config).analyze()

        assert outcome.has_data is False
        assert outcome.results == []
        assert outcome.run_id is None

        async with database.session() as session:
            runs = (
                await session.execute(select(func.count()).select_from(NicheScoreRun))
            ).scalar_one()
        assert runs == 0, "an empty analysis must not leave a run behind"


class TestScoringRun:
    async def test_persists_run_and_scores(
        self, database, column_mapping, niche_score_config, xlsx_factory
    ) -> None:
        await import_export(database, column_mapping, xlsx_factory, "2026-09-01", name="sep.xlsx")

        outcome = await NicheAnalysisService(database, niche_score_config).analyze()

        assert outcome.has_data
        assert outcome.persisted
        assert len(outcome.scored) == 2
        assert [r.rank for r in outcome.scored] == [1, 2]

        async with database.session() as session:
            run = await NicheScoreRepository(session).latest_run()
            assert run is not None
            assert run.score_version == "niche_score_v1"
            assert run.niches_scored == 2
            assert run.dataset["channels"] == 6
            assert run.config["weights"]

            scores = (
                (await session.execute(select(NicheScore).where(NicheScore.run_id == run.id)))
                .scalars()
                .all()
            )
        assert len(scores) == 2
        assert all(score.components for score in scores)
        assert all(score.metrics["median_subscribers"] for score in scores)

    async def test_medians_are_computed_over_the_latest_snapshot(
        self, database, column_mapping, niche_score_config, xlsx_factory
    ) -> None:
        await import_export(database, column_mapping, xlsx_factory, "2026-09-01", name="sep.xlsx")
        await import_export(
            database, column_mapping, xlsx_factory, "2026-10-01", factor=2.0, name="oct.xlsx"
        )

        outcome = await NicheAnalysisService(database, niche_score_config).analyze()
        by_slug = {r.slug: r for r in outcome.results}
        ai = by_slug["ai-i-tehnologii"]

        # Latest snapshot doubled every channel: 20k / 40k / 60k -> median 40k.
        assert ai.metrics.median_subscribers == 40_000
        assert ai.metrics.p25_subscribers == 30_000
        assert ai.metrics.p75_subscribers == 50_000
        assert ai.metrics.channels_count == 3

    async def test_as_of_limits_the_snapshots_used(
        self, database, column_mapping, niche_score_config, xlsx_factory
    ) -> None:
        await import_export(database, column_mapping, xlsx_factory, "2026-09-01", name="sep.xlsx")
        await import_export(
            database, column_mapping, xlsx_factory, "2026-10-01", factor=2.0, name="oct.xlsx"
        )

        service = NicheAnalysisService(database, niche_score_config)
        outcome = await service.analyze(as_of=date(2026, 9, 15))

        ai = {r.slug: r for r in outcome.results}["ai-i-tehnologii"]
        assert ai.metrics.median_subscribers == 20_000, "October data must be excluded"

    async def test_no_persist_leaves_no_run(
        self, database, column_mapping, niche_score_config, xlsx_factory
    ) -> None:
        await import_export(database, column_mapping, xlsx_factory, "2026-09-01", name="sep.xlsx")

        outcome = await NicheAnalysisService(database, niche_score_config).analyze(persist=False)

        assert outcome.persisted is False
        assert outcome.run_id is None
        async with database.session() as session:
            runs = (
                await session.execute(select(func.count()).select_from(NicheScoreRun))
            ).scalar_one()
        assert runs == 0


class TestComponentAvailability:
    async def test_growth_is_unavailable_with_a_single_snapshot_date(
        self, database, column_mapping, niche_score_config, xlsx_factory
    ) -> None:
        await import_export(database, column_mapping, xlsx_factory, "2026-09-01", name="sep.xlsx")

        outcome = await NicheAnalysisService(database, niche_score_config).analyze()

        assert all("growth_potential" in r.unavailable_components for r in outcome.scored)
        assert all(r.score is not None for r in outcome.scored), (
            "an unavailable component must renormalize weights, not void the score"
        )

    async def test_growth_becomes_available_after_a_second_date(
        self, database, column_mapping, niche_score_config, xlsx_factory
    ) -> None:
        await import_export(database, column_mapping, xlsx_factory, "2026-09-01", name="sep.xlsx")
        await import_export(
            database, column_mapping, xlsx_factory, "2026-10-01", factor=1.5, name="oct.xlsx"
        )

        outcome = await NicheAnalysisService(database, niche_score_config).analyze()

        assert all("growth_potential" not in r.unavailable_components for r in outcome.scored)
        ai = {r.slug: r for r in outcome.results}["ai-i-tehnologii"]
        assert ai.metrics.median_growth_rate == pytest.approx(0.5, abs=0.01)

    async def test_concentration_is_unavailable_when_the_top_n_are_the_whole_niche(
        self, database, column_mapping, niche_score_config, xlsx_factory
    ) -> None:
        """Three channels and a top-3 measure would always report 100% competition."""
        await import_export(database, column_mapping, xlsx_factory, "2026-09-01", name="sep.xlsx")

        outcome = await NicheAnalysisService(database, niche_score_config).analyze()

        for result in outcome.scored:
            assert result.metrics.channels_count == 3
            assert result.metrics.top_channel_share is None
            assert "low_competition" in result.unavailable_components
            assert result.score is not None

    async def test_concentration_is_computed_when_the_niche_is_wider(
        self, database, column_mapping, niche_score_config, xlsx_factory
    ) -> None:
        rows = build_rows_for("2026-09-01")
        rows.append(
            [
                "AI Weekly",
                "https://t.me/aiweekly",
                AI,
                "40000",
                "19,0%",
                "9000",
                "1,20",
                "50000",
                "2026-09-01",
            ]
        )
        service = DirectImportService(database, column_mapping)
        await service.import_file(xlsx_factory(HEADERS, rows, name="wide.xlsx"))

        outcome = await NicheAnalysisService(database, niche_score_config).analyze()
        ai = {r.slug: r for r in outcome.results}["ai-i-tehnologii"]

        assert ai.metrics.channels_count == 4
        assert ai.metrics.top_channel_share is not None
        assert 0 < float(ai.metrics.top_channel_share) < 1

    async def test_thin_niche_is_flagged_and_excluded(
        self, database, column_mapping, niche_score_config, xlsx_factory
    ) -> None:
        rows = build_rows_for("2026-09-01")
        rows.append(
            [
                "Solo Finance",
                "https://t.me/solofin",
                "Финансы",
                "4000",
                "8,0%",
                "500",
                "0,40",
                "5000",
                "2026-09-01",
            ]
        )
        service = DirectImportService(database, column_mapping)
        await service.import_file(xlsx_factory(HEADERS, rows, name="with-thin.xlsx"))

        outcome = await NicheAnalysisService(database, niche_score_config).analyze()

        skipped = {r.slug for r in outcome.skipped}
        assert "finansy" in skipped
        assert all(r.insufficient_data for r in outcome.skipped)
        assert len(outcome.scored) == 2


MIXED_HEADERS = HEADERS
# The same three niches on both platforms, so a per-platform ranking is possible.
MIXED_CHANNELS = [
    ("TG AI 1", "https://t.me/tgai1", AI, 10_000, "25,0%", 3_000, "1,50", 20_000),
    ("TG AI 2", "https://t.me/tgai2", AI, 20_000, "22,0%", 5_000, "1,40", 30_000),
    ("TG AI 3", "https://t.me/tgai3", AI, 30_000, "20,0%", 7_000, "1,30", 40_000),
    ("TG Career 1", "https://t.me/tgc1", CAREER, 5_000, "12,0%", 900, "0,60", 6_000),
    ("TG Career 2", "https://t.me/tgc2", CAREER, 6_000, "11,0%", 1_000, "0,55", 7_000),
    ("TG Career 3", "https://t.me/tgc3", CAREER, 7_000, "10,0%", 1_100, "0,50", 8_000),
    ("MAX AI 1", "https://max.ru/mxai1", AI, 4_000, "9,0%", 400, "0,40", 4_000),
    ("MAX AI 2", "https://max.ru/mxai2", AI, 5_000, "8,0%", 500, "0,35", 5_000),
    ("MAX AI 3", "https://max.ru/mxai3", AI, 6_000, "7,0%", 600, "0,30", 6_000),
    ("MAX Career 1", "https://max.ru/mxc1", CAREER, 9_000, "24,0%", 2_500, "1,60", 25_000),
    ("MAX Career 2", "https://max.ru/mxc2", CAREER, 11_000, "26,0%", 3_000, "1,70", 28_000),
    ("MAX Career 3", "https://max.ru/mxc3", CAREER, 13_000, "28,0%", 3_500, "1,80", 31_000),
]


def mixed_rows(snapshot_date: str = "2026-09-01") -> list[list[object]]:
    return [
        [name, url, category, str(subs), err, str(views), cpv, str(price), snapshot_date]
        for name, url, category, subs, err, views, cpv, price in MIXED_CHANNELS
    ]


class TestPlatformScoping:
    async def test_platform_run_only_covers_that_platform(
        self, database, column_mapping, niche_score_config, xlsx_factory
    ) -> None:
        service = DirectImportService(database, column_mapping)
        await service.import_file(xlsx_factory(MIXED_HEADERS, mixed_rows(), name="mixed.xlsx"))

        analysis = NicheAnalysisService(database, niche_score_config)
        telegram = await analysis.analyze(platform=Platform.TELEGRAM)
        maximum = await analysis.analyze(platform=Platform.MAX)

        assert telegram.dataset.channels == 6
        assert maximum.dataset.channels == 6
        assert telegram.platform is Platform.TELEGRAM
        assert {r.metrics.channels_count for r in telegram.scored} == {3}

    async def test_percentiles_are_computed_inside_the_platform(
        self, database, column_mapping, niche_score_config, xlsx_factory
    ) -> None:
        """AI leads on Telegram while Career leads on MAX, by construction."""
        service = DirectImportService(database, column_mapping)
        await service.import_file(xlsx_factory(MIXED_HEADERS, mixed_rows(), name="mixed.xlsx"))

        analysis = NicheAnalysisService(database, niche_score_config)
        telegram = await analysis.analyze(platform=Platform.TELEGRAM)
        maximum = await analysis.analyze(platform=Platform.MAX)

        assert telegram.scored[0].slug == "ai-i-tehnologii"
        assert maximum.scored[0].slug == "karera"

    async def test_runs_are_stored_and_fetched_per_scope(
        self, database, column_mapping, niche_score_config, xlsx_factory
    ) -> None:
        service = DirectImportService(database, column_mapping)
        await service.import_file(xlsx_factory(MIXED_HEADERS, mixed_rows(), name="mixed.xlsx"))

        analysis = NicheAnalysisService(database, niche_score_config)
        await analysis.analyze()
        await analysis.analyze(platform=Platform.TELEGRAM)
        await analysis.analyze(platform=Platform.MAX)

        async with database.session() as session:
            repo = NicheScoreRepository(session)
            overall = await repo.latest_run()
            telegram = await repo.latest_run(platform=Platform.TELEGRAM)
            maximum = await repo.latest_run(platform=Platform.MAX)

        assert overall is not None and overall.platform is None
        assert telegram is not None and telegram.platform is Platform.TELEGRAM
        assert maximum is not None and maximum.platform is Platform.MAX
        assert overall.dataset["channels"] == 12
        assert telegram.dataset["channels"] == 6

    async def test_platform_reports_do_not_overwrite_the_overall_ones(
        self, database, column_mapping, niche_score_config, xlsx_factory, tmp_path: Path
    ) -> None:
        service = DirectImportService(database, column_mapping)
        await service.import_file(xlsx_factory(MIXED_HEADERS, mixed_rows(), name="mixed.xlsx"))

        analysis = NicheAnalysisService(database, niche_score_config)
        await analysis.analyze()
        await analysis.analyze(platform=Platform.TELEGRAM)

        written: list[str] = []
        async with database.session() as session:
            repo = NicheScoreRepository(session)
            for platform in (None, Platform.TELEGRAM):
                run = await repo.latest_run(platform=platform)
                rows = build_rows(await repo.scores_for_run(run.id))
                written += [path.name for path in write_reports(tmp_path, run, rows)]

        assert NICHE_RANKING_FILE in written
        assert "niche-ranking-telegram.md" in written
        assert "market-overview-telegram.md" in written


class TestCrossPlatform:
    async def test_ranks_by_the_weaker_platform(
        self, database, column_mapping, niche_score_config, xlsx_factory
    ) -> None:
        service = DirectImportService(database, column_mapping)
        await service.import_file(xlsx_factory(MIXED_HEADERS, mixed_rows(), name="mixed.xlsx"))

        analysis = NicheAnalysisService(database, niche_score_config)
        telegram = await analysis.analyze(platform=Platform.TELEGRAM)
        maximum = await analysis.analyze(platform=Platform.MAX)

        async with database.session() as session:
            repo = NicheScoreRepository(session)
            telegram_rows = build_rows(await repo.scores_for_run(telegram.run_id))
            max_rows = build_rows(await repo.scores_for_run(maximum.run_id))

        compared = compare_platforms(telegram_rows, max_rows)

        assert all(niche.on_both for niche in compared)
        for niche in compared:
            assert niche.weakest_score == min(niche.score_telegram, niche.score_max)
        scores = [niche.weakest_score for niche in compared]
        assert scores == sorted(scores, reverse=True), "strongest weakest-link first"

    async def test_niche_present_on_one_platform_only_is_separated(
        self, database, column_mapping, niche_score_config, xlsx_factory
    ) -> None:
        rows = mixed_rows()
        rows += [
            [f"TG Only {i}", f"https://t.me/tgonly{i}", "Финансы", "8000", "9,0%", "800",
             "0,70", "9000", "2026-09-01"]
            for i in range(3)
        ]
        service = DirectImportService(database, column_mapping)
        await service.import_file(xlsx_factory(MIXED_HEADERS, rows, name="mixed.xlsx"))

        analysis = NicheAnalysisService(database, niche_score_config)
        telegram = await analysis.analyze(platform=Platform.TELEGRAM)
        maximum = await analysis.analyze(platform=Platform.MAX)

        async with database.session() as session:
            repo = NicheScoreRepository(session)
            telegram_rows = build_rows(await repo.scores_for_run(telegram.run_id))
            max_rows = build_rows(await repo.scores_for_run(maximum.run_id))

        compared = compare_platforms(telegram_rows, max_rows)
        single = [niche for niche in compared if not niche.on_both]

        assert [niche.slug for niche in single] == ["finansy"]
        assert single[0].score_max is None
        assert single[0].weakest_score is None, "no dual-platform score without both platforms"


class TestReports:
    async def test_reports_are_written_from_the_stored_run(
        self, database, column_mapping, niche_score_config, xlsx_factory, tmp_path: Path
    ) -> None:
        await import_export(database, column_mapping, xlsx_factory, "2026-09-01", name="sep.xlsx")
        outcome = await NicheAnalysisService(database, niche_score_config).analyze()

        async with database.session() as session:
            repo = NicheScoreRepository(session)
            run = await repo.latest_run()
            rows = build_rows(await repo.scores_for_run(outcome.run_id))

        written = write_reports(tmp_path / "reports", run, rows)

        assert {path.name for path in written} == {
            MARKET_OVERVIEW_FILE,
            NICHE_RANKING_FILE,
            NICHE_RANKING_CSV,
        }

        ranking = (tmp_path / "reports" / NICHE_RANKING_FILE).read_text(encoding="utf-8")
        assert str(run.id) in ranking
        assert "niche_score_v1" in ranking
        assert "Субъективные оценки не заданы" in ranking, "caveats must be visible"
        assert "growth_potential" in ranking, "unavailable components must be named"

        overview = (tmp_path / "reports" / MARKET_OVERVIEW_FILE).read_text(encoding="utf-8")
        assert "Yandex Direct export" in overview, "the report must name its data source"

        csv_content = (tmp_path / "reports" / NICHE_RANKING_CSV).read_text(encoding="utf-8")
        assert csv_content.splitlines()[0].startswith("rank,slug,name,score")
        assert len(csv_content.splitlines()) == 3
