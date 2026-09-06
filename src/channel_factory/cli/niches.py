"""Niche analytics commands."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import typer

from channel_factory.cli.app import app
from channel_factory.core.config import get_settings
from channel_factory.core.enums import Platform
from channel_factory.core.logging import setup_logging
from channel_factory.db.repositories.niches import NicheScoreRepository
from channel_factory.db.session import Database
from channel_factory.niches.config import NicheScoreConfigError, load_niche_score_config
from channel_factory.niches.cross_platform import (
    compare_platforms,
    write_cross_platform_report,
)
from channel_factory.niches.reports import build_rows, scope_label, write_reports
from channel_factory.niches.service import AnalysisOutcome, NicheAnalysisService

NO_DATA_MESSAGE = (
    "NO DATA: в базе нет рыночных снимков. Импортируйте выгрузку командой "
    "import-direct, затем повторите анализ."
)


def _load_config():
    settings = get_settings()
    try:
        return load_niche_score_config(settings.niche_score_config)
    except NicheScoreConfigError as exc:
        typer.secho(f"Niche score config error: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc


def _parse_as_of(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        typer.secho(f"Invalid --as-of: {value}", fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc


def _scope_text(platform: Platform | None) -> str:
    return "все платформы" if platform is None else platform.value


@app.command("analyze-niches")
def analyze_niches(
    as_of: str | None = typer.Option(
        None, "--as-of", help="Считать по снимкам не позднее этой даты (YYYY-MM-DD)"
    ),
    platform: Platform | None = typer.Option(
        None, "--platform", case_sensitive=False, help="Ограничить анализ одной платформой"
    ),
    persist: bool = typer.Option(
        True, "--persist/--no-persist", help="Сохранять прогон в базу"
    ),
    report: bool = typer.Option(
        False, "--report", help="Сразу сгенерировать файлы отчётов в reports/"
    ),
) -> None:
    """Посчитать метрики ниш и Niche Score, сохранить прогон."""
    settings = get_settings()
    setup_logging(settings.log_level)
    config = _load_config()
    outcome = asyncio.run(_analyze(config, _parse_as_of(as_of), persist, report, platform))

    if not outcome.has_data:
        typer.secho(NO_DATA_MESSAGE, fg=typer.colors.YELLOW)
        raise typer.Exit(code=0)


async def _analyze(
    config,
    as_of: date | None,
    persist: bool,
    report: bool,
    platform: Platform | None,
) -> AnalysisOutcome:
    settings = get_settings()
    database = Database(settings.database_url)
    try:
        service = NicheAnalysisService(database, config)
        outcome = await service.analyze(as_of=as_of, persist=persist, platform=platform)
        if not outcome.has_data:
            return outcome

        _print_outcome(outcome)

        if report and outcome.run_id is not None:
            async with database.session() as session:
                repo = NicheScoreRepository(session)
                run = await repo.latest_run(platform=platform)
                rows = build_rows(await repo.scores_for_run(outcome.run_id))
                written = write_reports(settings.reports_dir, run, rows) if run else []
            typer.echo("\nОтчёты:")
            for path in written:
                typer.echo(f"  {path}")
        elif report:
            typer.secho(
                "\nОтчёты не сгенерированы: прогон не сохранён (--no-persist).",
                fg=typer.colors.YELLOW,
            )
        return outcome
    finally:
        await database.dispose()


def _print_outcome(outcome: AnalysisOutcome) -> None:
    dataset = outcome.dataset
    typer.echo(f"Версия формулы : {outcome.score_version}")
    typer.echo(f"Охват          : {_scope_text(outcome.platform)}")
    typer.echo(
        f"Данные         : {dataset.channels} каналов, {dataset.snapshots} снимков, "
        f"{dataset.snapshot_dates} дат ({dataset.first_date} — {dataset.last_date})"
    )
    if dataset.sources:
        typer.echo(f"Источники      : {', '.join(dataset.sources)}")
    if dataset.channels_without_niche:
        typer.echo(f"Без категории  : {dataset.channels_without_niche} каналов")
    if outcome.run_id:
        typer.echo(f"Прогон         : {outcome.run_id}")

    scored = outcome.scored
    typer.echo(f"\nРанжировано ниш: {len(scored)}, исключено: {len(outcome.skipped)}\n")

    if scored:
        typer.echo(f"{'#':>2}  {'SCORE':>6}  {'КАНАЛОВ':>7}  НИША")
        typer.echo("-" * 60)
        for result in scored:
            typer.echo(
                f"{result.rank:>2}  {result.score:>6.1f}  "
                f"{result.metrics.channels_count:>7}  {result.name}"
            )

    if outcome.skipped:
        typer.secho(
            "\nИсключены (мало каналов): "
            + ", ".join(
                f"{r.name} ({r.metrics.channels_count})" for r in outcome.skipped[:12]
            ),
            fg=typer.colors.YELLOW,
        )

    if outcome.low_confidence:
        typer.secho(
            "\nВНИМАНИЕ: ниш слишком мало для надёжного перцентильного ранжирования — "
            "разница в несколько пунктов не значима.",
            fg=typer.colors.YELLOW,
        )

    defaults = [r for r in scored if r.uses_only_default_ratings]
    if defaults:
        typer.secho(
            f"ВНИМАНИЕ: субъективные оценки не заданы для {len(defaults)} ниш "
            "(используются нейтральные 50) — заполните manual_overrides "
            "в config/niche_score.yaml.",
            fg=typer.colors.YELLOW,
        )

    # Counted per niche: a component missing for one thin niche is a very
    # different statement from one missing for the whole dataset.
    unavailable: dict[str, int] = {}
    for result in scored:
        for key in result.unavailable_components:
            unavailable[key] = unavailable.get(key, 0) + 1
    if unavailable:
        listed = ", ".join(
            f"{key} ({count} из {len(scored)} ниш)"
            for key, count in sorted(unavailable.items())
        )
        typer.echo(f"Недоступные компоненты (веса перераспределены): {listed}")


@app.command("show-top-niches")
def show_top_niches(
    limit: int = typer.Option(10, "--limit", min=1, max=100),
    platform: Platform | None = typer.Option(
        None, "--platform", case_sensitive=False, help="Показать прогон одной платформы"
    ),
) -> None:
    """Показать последнее сохранённое ранжирование ниш."""
    settings = get_settings()
    setup_logging(settings.log_level)
    asyncio.run(_show_top(limit, platform))


async def _show_top(limit: int, platform: Platform | None) -> None:
    database = Database(get_settings().database_url)
    try:
        async with database.session() as session:
            repo = NicheScoreRepository(session)
            run = await repo.latest_run(platform=platform)
            if run is None:
                typer.secho(
                    f"NO DATA: нет сохранённого прогона для охвата "
                    f"'{_scope_text(platform)}'. Запустите analyze-niches.",
                    fg=typer.colors.YELLOW,
                )
                return
            rows = build_rows(await repo.scores_for_run(run.id))

        typer.echo(f"Прогон  : {run.id} ({run.created_at:%Y-%m-%d %H:%M} UTC)")
        typer.echo(f"Формула : {run.score_version}")
        typer.echo(f"Охват   : {scope_label(run)}")
        if run.as_of_date:
            typer.echo(f"На дату : {run.as_of_date}")
        typer.echo("")

        scored = [row for row in rows if row.score is not None][:limit]
        if not scored:
            typer.secho("Нет ниш с достаточным количеством данных.", fg=typer.colors.YELLOW)
            return

        typer.echo(f"{'#':>2}  {'SCORE':>6}  {'КАНАЛОВ':>7}  НИША")
        typer.echo("-" * 60)
        for row in scored:
            typer.echo(f"{row.rank:>2}  {row.score:>6.1f}  {row.channels_count:>7}  {row.name}")

        if run.low_confidence:
            typer.secho(
                "\nВНИМАНИЕ: мало ниш — ранжирование низкой достоверности.",
                fg=typer.colors.YELLOW,
            )
    finally:
        await database.dispose()


@app.command("niche-report")
def niche_report(
    output: Path | None = typer.Option(
        None, "--output", help="Каталог для отчётов (по умолчанию reports/)"
    ),
    platform: Platform | None = typer.Option(
        None, "--platform", case_sensitive=False, help="Отчёты по прогону одной платформы"
    ),
) -> None:
    """Сгенерировать отчёты из последнего сохранённого прогона."""
    settings = get_settings()
    setup_logging(settings.log_level)
    asyncio.run(_write_reports(output or settings.reports_dir, platform))


async def _write_reports(directory: Path, platform: Platform | None) -> None:
    database = Database(get_settings().database_url)
    try:
        async with database.session() as session:
            repo = NicheScoreRepository(session)
            run = await repo.latest_run(platform=platform)
            if run is None:
                typer.secho(
                    f"NO DATA: нет сохранённых прогонов для охвата "
                    f"'{_scope_text(platform)}'. Запустите analyze-niches.",
                    fg=typer.colors.YELLOW,
                )
                return
            rows = build_rows(await repo.scores_for_run(run.id))

        written = write_reports(directory, run, rows)
        typer.echo(f"Прогон: {run.id} ({run.score_version}, {scope_label(run)})")
        for path in written:
            typer.echo(f"  {path}")
    finally:
        await database.dispose()


@app.command("cross-platform-niches")
def cross_platform_niches(
    limit: int = typer.Option(15, "--limit", min=1, max=100),
    output: Path | None = typer.Option(
        None, "--output", help="Каталог для отчёта (по умолчанию reports/)"
    ),
) -> None:
    """Сравнить ранжирования Telegram и MAX: где ниша сильна сразу на обеих."""
    settings = get_settings()
    setup_logging(settings.log_level)
    asyncio.run(_cross_platform(limit, output or settings.reports_dir))


async def _cross_platform(limit: int, directory: Path) -> None:
    database = Database(get_settings().database_url)
    try:
        async with database.session() as session:
            repo = NicheScoreRepository(session)
            telegram_run = await repo.latest_run(platform=Platform.TELEGRAM)
            max_run = await repo.latest_run(platform=Platform.MAX)
            missing = [
                name
                for name, run in (("TELEGRAM", telegram_run), ("MAX", max_run))
                if run is None
            ]
            if missing:
                typer.secho(
                    f"NO DATA: нет прогонов для {', '.join(missing)}. "
                    "Запустите analyze-niches --platform TELEGRAM и "
                    "analyze-niches --platform MAX.",
                    fg=typer.colors.YELLOW,
                )
                return

            telegram_rows = build_rows(await repo.scores_for_run(telegram_run.id))
            max_rows = build_rows(await repo.scores_for_run(max_run.id))

        niches = compare_platforms(telegram_rows, max_rows)
        both = [niche for niche in niches if niche.on_both]

        typer.echo("Ниша оценивается по слабейшей из двух платформ: единый бренд не может")
        typer.echo("быть сильнее своего слабого звена.\n")
        typer.echo(f"{'#':>2}  {'СЛАБЕЙШАЯ':>10}  {'TG':>6}  {'MAX':>6}  {'РАЗРЫВ':>7}  НИША")
        typer.echo("-" * 74)
        for position, niche in enumerate(both[:limit], start=1):
            flag = " !" if niche.is_asymmetric else ""
            typer.echo(
                f"{position:>2}  {niche.weakest_score:>10.1f}  {niche.score_telegram:>6.1f}  "
                f"{niche.score_max:>6.1f}  {niche.gap:>7.1f}  {niche.name}{flag}"
            )

        single = [niche for niche in niches if not niche.on_both]
        if single:
            typer.secho(
                f"\nРанжированы только на одной платформе: {len(single)} ниш "
                "(на второй слишком мало каналов для устойчивой статистики).",
                fg=typer.colors.YELLOW,
            )

        path = write_cross_platform_report(directory, telegram_run, max_run, niches)
        typer.echo(f"\nОтчёт: {path}")
    finally:
        await database.dispose()
