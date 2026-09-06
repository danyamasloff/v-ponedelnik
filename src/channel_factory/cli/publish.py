"""Publishing commands: preview, connection check, and guarded posting."""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta

import typer
from sqlalchemy import select

from channel_factory.cli.app import app
from channel_factory.content.brand import load_brand
from channel_factory.content.evergreen import load_topics
from channel_factory.content.factory import build_analysis_generator
from channel_factory.core.config import get_settings
from channel_factory.core.enums import ClusterStatus, PublicationStatus, PublishMode
from channel_factory.core.logging import setup_logging
from channel_factory.db.models import Publication
from channel_factory.db.repositories.research import ResearchClusterRepository
from channel_factory.db.session import Database
from channel_factory.publishers.base import PublishError
from channel_factory.publishers.max.client import MaxApiClient
from channel_factory.publishers.max.factory import build_publisher
from channel_factory.publishers.scheduler import (
    OCCUPYING_STATUSES,
    BreakingRules,
    PostingScheduler,
    SchedulerAction,
    parse_own_slots,
    parse_slots,
)
from channel_factory.publishers.service import PublishingService

STATUS_COLORS = {
    PublicationStatus.PUBLISHED: typer.colors.GREEN,
    PublicationStatus.SIMULATED: typer.colors.BLUE,
    PublicationStatus.BLOCKED: typer.colors.YELLOW,
    PublicationStatus.FAILED: typer.colors.RED,
}


def _database() -> Database:
    return Database(get_settings().database_url)


def _scheduler(database: Database, service: PublishingService) -> PostingScheduler:
    """Scheduler wired from settings, including which slots are ours to write."""
    settings = get_settings()
    return PostingScheduler(
        database,
        service,
        slot_times=parse_slots(settings.publish_slots),
        window=timedelta(minutes=settings.publish_slot_window_minutes),
        own_slot_indexes=parse_own_slots(settings.publish_own_slots),
        topics_path=settings.evergreen_topics_config,
        max_topic_age=timedelta(hours=settings.publish_max_topic_age_hours),
        breaking=BreakingRules(
            enabled=settings.breaking_enabled,
            min_score=settings.breaking_min_score,
            max_age=timedelta(minutes=settings.breaking_max_age_minutes),
            min_sources=settings.breaking_min_sources,
            max_per_day=settings.breaking_max_per_day,
            min_gap=timedelta(minutes=settings.breaking_min_gap_minutes),
        ),
    )


async def _service(database: Database) -> tuple[MaxApiClient, PublishingService]:
    """Service plus the HTTP client it owns; the caller closes the client.

    A dry run must work before a token exists, so the publisher is built in
    rehearsal mode whenever the kill switch says nothing will be sent.
    """
    settings = get_settings()
    rehearsal = settings.publish_mode is not PublishMode.LIVE or not settings.auto_publish_enabled
    client, publisher = build_publisher(rehearsal=rehearsal)
    service = PublishingService(
        database,
        publisher,
        channel_ref=str(settings.max_chat_id) if settings.max_chat_id is not None else None,
        mode=settings.publish_mode,
        auto_publish_enabled=settings.auto_publish_enabled,
        generator=await build_analysis_generator(database, settings),
        cards_dir=settings.cards_dir,
        brand=load_brand(settings.brand_config),
    )
    return client, service


@app.command("publish-preview")
def publish_preview(
    limit: int = typer.Option(3, "--limit", min=1, max=20),
) -> None:
    """Показать, как выглядели бы посты по отобранным темам. Ничего не отправляет."""
    settings = get_settings()
    setup_logging(settings.log_level)
    asyncio.run(_preview(limit))


async def _preview(limit: int) -> None:
    database = _database()
    client = None
    try:
        client, service = await _service(database)
        async with database.session() as session:
            clusters = await ResearchClusterRepository(session).top(
                statuses=[ClusterStatus.SELECTED], limit=limit
            )
        if not clusters:
            typer.secho(
                "NO DATA: нет отобранных тем. Запустите research-poll, research-cluster "
                "и score-topics.",
                fg=typer.colors.YELLOW,
            )
            return

        for index, cluster in enumerate(clusters, start=1):
            draft = await service.draft_for_cluster(cluster)
            typer.echo("=" * 72)
            score = "—" if cluster.topic_score is None else f"{float(cluster.topic_score):.1f}"
            typer.echo(f"{index}. Score {score} | {draft.length} символов | MAX")
            typer.echo("=" * 72)
            typer.echo(draft.text)
            typer.echo("")
            if not draft.is_publishable:
                typer.secho(
                    "  ⚠ Черновик, а не готовый пост: оригинальный разбор пишет "
                    "PHASE 5. Публикация такого текста заблокирована.",
                    fg=typer.colors.YELLOW,
                )
            typer.echo("")
    finally:
        if client is not None:
            await client.aclose()
        await database.dispose()


@app.command("publish-max")
def publish_max(
    cluster_id: str = typer.Argument(..., help="UUID кластера (см. show-clusters)"),
) -> None:
    """Опубликовать тему в MAX. Уважает PUBLISH_MODE и AUTO_PUBLISH_ENABLED."""
    settings = get_settings()
    setup_logging(settings.log_level)
    try:
        parsed = uuid.UUID(cluster_id)
    except ValueError as exc:
        typer.secho(f"Не UUID: {cluster_id}", fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc
    asyncio.run(_publish(parsed))


async def _publish(cluster_id: uuid.UUID) -> None:
    settings = get_settings()
    database = _database()
    client = None
    try:
        client, service = await _service(database)
        typer.echo(f"Режим      : {settings.publish_mode.value}")
        typer.echo(f"Автопостинг: {'включён' if settings.auto_publish_enabled else 'выключен'}")

        outcome = await service.publish_cluster(cluster_id)

        typer.secho(
            f"Статус     : {outcome.status.value}",
            fg=STATUS_COLORS.get(outcome.status),
        )
        if outcome.reason:
            typer.echo(f"Причина    : {outcome.reason}")
        for problem in outcome.problems or []:
            typer.secho(f"  ! {problem}", fg=typer.colors.YELLOW)
        if outcome.external_message_id:
            typer.echo(f"ID сообщения: {outcome.external_message_id}")
        typer.echo(f"Запись     : {outcome.publication_id}")
        typer.echo("\n--- текст ---")
        typer.echo(outcome.draft.text)
    except PublishError as exc:
        typer.secho(f"Ошибка: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc
    finally:
        if client is not None:
            await client.aclose()
        await database.dispose()


@app.command("publish-log")
def publish_log(limit: int = typer.Option(15, "--limit", min=1, max=100)) -> None:
    """История публикаций, включая симулированные."""
    settings = get_settings()
    setup_logging(settings.log_level)
    asyncio.run(_publish_log(limit))


async def _publish_log(limit: int) -> None:
    database = _database()
    client = None
    try:
        client, service = await _service(database)
        rows = await service.recent(limit)
        if not rows:
            typer.echo("Публикаций ещё не было.")
            return
        typer.echo(f"{'КОГДА':17} {'ПЛАТФОРМА':10} {'РЕЖИМ':8} {'СТАТУС':10} ТЕКСТ")
        typer.echo("-" * 100)
        for row in rows:
            preview = row.text.replace("\n", " ")[:44]
            typer.secho(
                f"{row.created_at:%Y-%m-%d %H:%M}  {row.platform.value:10} "
                f"{row.mode.value:8} {row.status.value:10} {preview}",
                fg=STATUS_COLORS.get(row.status),
            )
    finally:
        if client is not None:
            await client.aclose()
        await database.dispose()


@app.command("publish-due")
def publish_due(
    now: str | None = typer.Option(
        None,
        "--now",
        help=(
            "Считать, что сейчас другое время, формат YYYY-MM-DDTHH:MM. Только для "
            "отладки: запись публикации всё равно получает реальное время, поэтому "
            "подменённый слот может остаться «свободным»"
        ),
    ),
) -> None:
    """Опубликовать пост, если открыт слот и он ещё не заполнен.

    Команда идемпотентна: её можно вызывать хоть каждые пять минут — в один
    слот уйдёт максимум один пост. Это и есть точка входа для планировщика ОС.
    """
    settings = get_settings()
    setup_logging(settings.log_level)
    moment = None
    if now:
        try:
            moment = datetime.fromisoformat(now).astimezone()
        except ValueError as exc:
            typer.secho(f"Не разобрал время: {now}", fg=typer.colors.RED)
            raise typer.Exit(code=2) from exc
    asyncio.run(_publish_due(moment))


async def _publish_due(moment: datetime | None) -> None:
    settings = get_settings()
    database = _database()
    client = None
    try:
        client, service = await _service(database)
        result = await _scheduler(database, service).run_once(moment)

        if result.slot is not None:
            typer.echo(f"Слот       : {result.slot.label}")
        typer.echo(f"Режим      : {settings.publish_mode.value}")
        typer.echo(f"Автопостинг: {'включён' if settings.auto_publish_enabled else 'выключен'}")

        if result.action is SchedulerAction.BREAKING:
            typer.secho("Срочная новость — публикуем вне слота", fg=typer.colors.MAGENTA)
        elif result.action is not SchedulerAction.ATTEMPTED:
            typer.secho(f"Действие   : {result.action.value}", fg=typer.colors.YELLOW)
            typer.echo(f"Причина    : {result.detail}")
            return

        outcome = result.outcome
        assert outcome is not None  # ATTEMPTED always carries an outcome
        typer.secho(f"Статус     : {outcome.status.value}", fg=STATUS_COLORS.get(outcome.status))
        if outcome.reason:
            typer.echo(f"Причина    : {outcome.reason}")
        for problem in outcome.problems or []:
            typer.secho(f"  ! {problem}", fg=typer.colors.YELLOW)
        if outcome.external_message_id:
            typer.echo(f"ID сообщения: {outcome.external_message_id}")
        typer.echo("")
        typer.echo("--- текст ---")
        typer.echo(outcome.draft.text)
    finally:
        if client is not None:
            await client.aclose()
        await database.dispose()


@app.command("publish-plan")
def publish_plan(
    days: int = typer.Option(3, "--days", min=1, max=30, help="На сколько дней вперёд"),
) -> None:
    """Показать слоты публикации и то, какие из них уже заполнены."""
    settings = get_settings()
    setup_logging(settings.log_level)
    asyncio.run(_publish_plan(days))


async def _publish_plan(days: int) -> None:
    settings = get_settings()
    database = _database()
    client = None
    try:
        client, service = await _service(database)
        scheduler = _scheduler(database, service)
        window = settings.publish_slot_window_minutes
        typer.echo(f"Слоты в день: {settings.publish_slots}  (окно {window} мин)")
        available = len(await _selected_unpublished(database))
        own_left = await _own_topics_left(scheduler)
        own_slots = sorted(parse_own_slots(settings.publish_own_slots))
        typer.echo(f"Новостных тем: {available}")
        typer.echo(f"Своих тем    : {own_left}")
        typer.echo(f"Свои слоты   : {own_slots if own_slots else 'нет'} (индексы в PUBLISH_SLOTS)")
        typer.echo("")
        for slot in await scheduler.upcoming(days=days):
            filled = await scheduler.slot_filled(slot)
            kind = "свой пост" if slot.index in scheduler.own_slot_indexes else "новость"
            mark = "занят" if filled else "свободен"
            color = typer.colors.GREEN if filled else typer.colors.YELLOW
            typer.secho(f"{slot.label}  {mark:<9} {kind}", fg=color)
    finally:
        if client is not None:
            await client.aclose()
        await database.dispose()


async def _own_topics_left(scheduler: PostingScheduler) -> int:
    """How many of our own topics are still unpublished."""
    try:
        topics = load_topics(get_settings().evergreen_topics_config)
    except Exception:
        return 0
    published = await scheduler.published_topic_keys()
    return sum(1 for topic in topics if topic.key not in published)


async def _selected_unpublished(database: Database) -> list[uuid.UUID]:
    """Topics that are selected and not yet posted — the supply for the plan."""
    async with database.session() as session:
        used = await session.execute(
            select(Publication.research_cluster_id).where(
                Publication.status.in_(OCCUPYING_STATUSES),
                Publication.research_cluster_id.is_not(None),
            )
        )
        published = {row for row in used.scalars().all() if row is not None}
        clusters = await ResearchClusterRepository(session).top(
            statuses=[ClusterStatus.SELECTED], limit=200
        )
    return [cluster.id for cluster in clusters if cluster.id not in published]


__all__ = ["PublishMode"]
