"""Niche analytics commands."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path

import typer

from channel_factory.cli.app import app
from channel_factory.core.config import get_settings
from channel_factory.core.logging import setup_logging
from channel_factory.db.repositories.niches import NicheScoreRepository
from channel_factory.db.session import Database
from channel_factory.niches.config import NicheScoreConfigError, load_niche_score_config
from channel_factory.niches.reports import build_rows, write_reports
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


@app.command("analyze-niches")
def analyze_niches(
    as_of: str | None = typer.Option(
        None, "--as-of", help="Считать по снимкам не позднее этой даты (YYYY-MM-DD)"
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
    outcome = asyncio.run(_analyze(config, _parse_as_of(as_of), persist, report))

    if not outcome.has_data:
        typer.secho(NO_DATA_MESSAGE, fg=typer.colors.YELLOW)
        raise typer.Exit(code=0)


async def _analyze(config, as_of: date | None, persist: bool, report: bool) -> AnalysisOutcome:
    settings = get_settings()
    database = Database(settings.database_url)
    try:
        service = NicheAnalysisService(database, config)
        outcome = await service.analyze(as_of=as_of, persist=persist)
        if not outcome.has_data:
            return outcome

        _print_outcome(outcome)

        if report and outcome.run_id is not None:
            async with database.session() as session:
                repo = NicheScoreRepository(session)
                run = await repo.latest_run()
                rows = build_rows(await repo.scores_for_run(outcome.run_id))
                if run is not None:
                    written = write_reports(settings.reports_dir, run, rows)
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

    for result in outcome.skipped:
        typer.secho(
            f"    пропущена: {result.name} (каналов: {result.metrics.channels_count})",
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

    unavailable = sorted({key for r in scored for key in r.unavailable_components})
    if unavailable:
        typer.echo(
            f"Недоступные компоненты (веса перераспределены): {', '.join(unavailable)}"
        )


@app.command("show-top-niches")
def show_top_niches(
    limit: int = typer.Option(10, "--limit", min=1, max=100),
) -> None:
    """Показать последнее сохранённое ранжирование ниш."""
    settings = get_settings()
    setup_logging(settings.log_level)
    asyncio.run(_show_top(limit))


async def _show_top(limit: int) -> None:
    database = Database(get_settings().database_url)
    try:
        async with database.session() as session:
            repo = NicheScoreRepository(session)
            run = await repo.latest_run()
            if run is None:
                typer.secho(
                    "NO DATA: ранжирование ещё не выполнялось. Запустите analyze-niches.",
                    fg=typer.colors.YELLOW,
                )
                return
            rows = build_rows(await repo.scores_for_run(run.id))

        typer.echo(f"Прогон  : {run.id} ({run.created_at:%Y-%m-%d %H:%M} UTC)")
        typer.echo(f"Формула : {run.score_version}")
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
) -> None:
    """Сгенерировать отчёты из последнего сохранённого прогона."""
    settings = get_settings()
    setup_logging(settings.log_level)
    asyncio.run(_write_reports(output or settings.reports_dir))


async def _write_reports(directory: Path) -> None:
    database = Database(get_settings().database_url)
    try:
        async with database.session() as session:
            repo = NicheScoreRepository(session)
            run = await repo.latest_run()
            if run is None:
                typer.secho(
                    "NO DATA: нет сохранённых прогонов. Запустите analyze-niches.",
                    fg=typer.colors.YELLOW,
                )
                return
            rows = build_rows(await repo.scores_for_run(run.id))

        written = write_reports(directory, run, rows)
        typer.echo(f"Прогон: {run.id} ({run.score_version})")
        for path in written:
            typer.echo(f"  {path}")
    finally:
        await database.dispose()
