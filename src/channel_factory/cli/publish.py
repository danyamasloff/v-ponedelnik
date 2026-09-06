"""Publishing commands: preview, connection check, and guarded posting."""

from __future__ import annotations

import asyncio
import uuid

import typer

from channel_factory.cli.app import app
from channel_factory.core.config import get_settings
from channel_factory.core.enums import ClusterStatus, PublicationStatus, PublishMode
from channel_factory.core.logging import setup_logging
from channel_factory.db.repositories.research import ResearchClusterRepository
from channel_factory.db.session import Database
from channel_factory.publishers.base import PublishError
from channel_factory.publishers.max.client import MaxApiClient
from channel_factory.publishers.max.factory import build_publisher
from channel_factory.publishers.service import PublishingService

STATUS_COLORS = {
    PublicationStatus.PUBLISHED: typer.colors.GREEN,
    PublicationStatus.SIMULATED: typer.colors.BLUE,
    PublicationStatus.BLOCKED: typer.colors.YELLOW,
    PublicationStatus.FAILED: typer.colors.RED,
}


def _database() -> Database:
    return Database(get_settings().database_url)


def _service(database: Database) -> tuple[MaxApiClient, PublishingService]:
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
        client, service = _service(database)
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
        client, service = _service(database)
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
        client, service = _service(database)
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


__all__ = ["PublishMode"]
