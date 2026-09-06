"""Competitor research commands."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path

import typer

from channel_factory.cli.app import app
from channel_factory.competitors.reports import (
    format_profile_line,
    scope_label,
    write_competitor_report,
)
from channel_factory.competitors.service import CompetitorService, NicheDossier
from channel_factory.core.config import get_settings
from channel_factory.core.enums import Platform
from channel_factory.core.logging import setup_logging
from channel_factory.db.session import Database

NO_DATA_MESSAGE = (
    "NO DATA: в базе нет рыночных данных. Импортируйте выгрузку командой import-direct."
)


def _database() -> Database:
    return Database(get_settings().database_url)


def _parse_uuid(value: str) -> uuid.UUID:
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        typer.secho(f"Не UUID: {value}", fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc


def _print_dossier(dossier: NicheDossier, limit: int) -> None:
    benchmarks = dossier.benchmarks
    typer.echo(f"Ниша    : {benchmarks.name} ({benchmarks.slug})")
    typer.echo(f"Охват   : {scope_label(benchmarks)}")
    typer.echo(f"Каналов : {benchmarks.channels_count}")
    typer.echo(f"Снимок  : {benchmarks.snapshot_date or '—'}")

    def as_int(value) -> str:
        return "—" if value is None else f"{round(float(value)):,}".replace(",", " ")

    def as_pct(value) -> str:
        return "—" if value is None else f"{float(value) * 100:.1f}%"

    typer.echo(
        f"\nПодписчики: p25 {as_int(benchmarks.subscribers_p25)} | "
        f"медиана {as_int(benchmarks.subscribers_p50)} | "
        f"p75 {as_int(benchmarks.subscribers_p75)} | "
        f"p90 {as_int(benchmarks.subscribers_p90)} | "
        f"max {as_int(benchmarks.subscribers_max)}"
    )
    typer.echo(
        f"ERR       : медиана {as_pct(benchmarks.err_p50)} | p75 {as_pct(benchmarks.err_p75)}"
    )
    typer.echo(
        f"Топ-{benchmarks.head_size} держат {as_pct(benchmarks.head_share)} подписчиков ниши"
    )

    typer.echo(f"\n--- Лидеры по подписчикам (топ-{limit}) ---")
    for position, profile in enumerate(dossier.by_subscribers, start=1):
        typer.echo(f"{position:>2}. {format_profile_line(profile)}")

    if dossier.engagement_floor:
        typer.echo(
            f"\n--- Лидеры по вовлечённости (от {as_int(dossier.engagement_floor)} "
            "подписчиков — медиана ниши) ---"
        )
    else:
        typer.echo(f"\n--- Лидеры по вовлечённости (топ-{limit}) ---")
    for position, profile in enumerate(dossier.by_engagement, start=1):
        typer.echo(f"{position:>2}. {format_profile_line(profile)}")

    if dossier.watched:
        typer.echo("\n--- На отслеживании ---")
        for profile in dossier.watched:
            typer.echo(f"    {format_profile_line(profile)}")

    typer.secho(
        "\nИсточник описывает рекламное предложение каналов, а не их контент: "
        "частоту публикаций, форматы и хуки по этим данным определить нельзя.",
        fg=typer.colors.YELLOW,
    )


@app.command("show-niche")
def show_niche(
    slug: str = typer.Argument(..., help="Slug ниши (см. show-top-niches или CSV отчёта)"),
    platform: Platform | None = typer.Option(
        None, "--platform", case_sensitive=False, help="Ограничить одной платформой"
    ),
    limit: int = typer.Option(10, "--limit", min=1, max=50),
    report: bool = typer.Option(False, "--report", help="Записать отчёт в reports/"),
    output: Path | None = typer.Option(None, "--output", help="Каталог для отчёта"),
) -> None:
    """Досье по нише: распределение, лидеры, пороги входа."""
    settings = get_settings()
    setup_logging(settings.log_level)
    asyncio.run(_show_niche(slug, platform, limit, report, output or settings.reports_dir))


async def _show_niche(
    slug: str,
    platform: Platform | None,
    limit: int,
    report: bool,
    directory: Path,
) -> None:
    database = _database()
    try:
        service = CompetitorService(database)
        dossier = await service.niche_dossier(slug, platform=platform, limit=limit)
        if dossier is None:
            if not await service.niche_exists(slug):
                typer.secho(f"Ниша '{slug}' не найдена.", fg=typer.colors.RED)
                known = await service.list_niche_slugs()
                if known:
                    typer.echo("\nДоступные ниши:")
                    for niche_slug, name in known:
                        typer.echo(f"  {niche_slug:36} {name}")
                else:
                    typer.secho(NO_DATA_MESSAGE, fg=typer.colors.YELLOW)
            else:
                typer.secho(
                    f"NO DATA: в нише '{slug}' нет каналов для выбранного охвата.",
                    fg=typer.colors.YELLOW,
                )
            raise typer.Exit(code=1)

        _print_dossier(dossier, limit)

        if report:
            path = write_competitor_report(directory, dossier)
            typer.echo(f"\nОтчёт: {path}")
    finally:
        await database.dispose()


@app.command("find-channel")
def find_channel(
    query: str = typer.Argument(..., help="Часть названия или ссылки канала"),
    limit: int = typer.Option(20, "--limit", min=1, max=100),
) -> None:
    """Найти канал в рыночных данных (нужен его id для watchlist)."""
    settings = get_settings()
    setup_logging(settings.log_level)
    asyncio.run(_find_channel(query, limit))


async def _find_channel(query: str, limit: int) -> None:
    database = _database()
    try:
        profiles = await CompetitorService(database).search(query, limit=limit)
        if not profiles:
            typer.secho(f"Ничего не найдено по '{query}'.", fg=typer.colors.YELLOW)
            return
        for profile in profiles:
            typer.echo(f"{profile.channel_id}  {format_profile_line(profile)}")
            if profile.channel_url:
                typer.echo(f"{'':38}{profile.channel_url}")
    finally:
        await database.dispose()


@app.command("watch-channel")
def watch_channel(
    channel_id: str = typer.Argument(..., help="UUID канала (см. find-channel)"),
    notes: str | None = typer.Option(None, "--notes", help="Зачем отслеживаем"),
) -> None:
    """Добавить канал в список отслеживаемых конкурентов."""
    settings = get_settings()
    setup_logging(settings.log_level)
    asyncio.run(_watch(_parse_uuid(channel_id), notes))


async def _watch(channel_id: uuid.UUID, notes: str | None) -> None:
    database = _database()
    try:
        profile = await CompetitorService(database).watch(channel_id, notes=notes)
        if profile is None:
            typer.secho(f"Канал {channel_id} не найден.", fg=typer.colors.RED)
            raise typer.Exit(code=1)
        typer.secho("Добавлен в watchlist:", fg=typer.colors.GREEN)
        typer.echo(f"  {format_profile_line(profile)}")
    finally:
        await database.dispose()


@app.command("unwatch-channel")
def unwatch_channel(
    channel_id: str = typer.Argument(..., help="UUID канала"),
) -> None:
    """Убрать канал из списка отслеживаемых."""
    settings = get_settings()
    setup_logging(settings.log_level)
    asyncio.run(_unwatch(_parse_uuid(channel_id)))


async def _unwatch(channel_id: uuid.UUID) -> None:
    database = _database()
    try:
        removed = await CompetitorService(database).unwatch(channel_id)
        if removed:
            typer.secho(f"Убран из watchlist: {channel_id}", fg=typer.colors.GREEN)
        else:
            typer.secho(f"Канал {channel_id} не был в watchlist.", fg=typer.colors.YELLOW)
    finally:
        await database.dispose()


@app.command("list-watchlist")
def list_watchlist() -> None:
    """Показать отслеживаемых конкурентов."""
    settings = get_settings()
    setup_logging(settings.log_level)
    asyncio.run(_list_watchlist())


async def _list_watchlist() -> None:
    database = _database()
    try:
        profiles = await CompetitorService(database).watchlist()
        if not profiles:
            typer.echo("Watchlist пуст. Добавьте канал командой watch-channel.")
            return
        for profile in profiles:
            typer.echo(f"{profile.channel_id}  {format_profile_line(profile)}")
            if profile.watch_notes:
                typer.echo(f"{'':38}note: {profile.watch_notes}")
    finally:
        await database.dispose()
