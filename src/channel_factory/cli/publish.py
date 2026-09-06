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
from channel_factory.publishers.max_publisher import MaxPublisher
from channel_factory.publishers.service import PublishingService

STATUS_COLORS = {
    PublicationStatus.PUBLISHED: typer.colors.GREEN,
    PublicationStatus.SIMULATED: typer.colors.BLUE,
    PublicationStatus.BLOCKED: typer.colors.YELLOW,
    PublicationStatus.FAILED: typer.colors.RED,
}


def _database() -> Database:
    return Database(get_settings().database_url)


def _service(database: Database) -> PublishingService:
    settings = get_settings()
    return PublishingService(
        database,
        MaxPublisher(settings.max_bot_token),
        channel_ref=settings.max_channel_id,
        mode=settings.publish_mode,
        auto_publish_enabled=settings.auto_publish_enabled,
    )


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
    try:
        service = _service(database)
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
        await database.dispose()


@app.command("max-check")
def max_check() -> None:
    """Проверить токен MAX и показать, в каких каналах бот уже есть."""
    settings = get_settings()
    setup_logging(settings.log_level)
    asyncio.run(_max_check())


async def _max_check() -> None:
    settings = get_settings()
    publisher = MaxPublisher(settings.max_bot_token)

    if not settings.max_bot_token:
        typer.secho("MAX_BOT_TOKEN не задан в .env.", fg=typer.colors.RED)
        typer.echo(
            "\nЧто нужно сделать:\n"
            "  1. Создать бота в кабинете MAX для партнёров.\n"
            "  2. Создать канал и добавить бота администратором.\n"
            "  3. Положить токен в .env как MAX_BOT_TOKEN.\n"
            "  4. Запустить max-check ещё раз — команда покажет chat_id канала."
        )
        raise typer.Exit(code=1)

    try:
        identity = await publisher.whoami()
        typer.secho("Токен рабочий.", fg=typer.colors.GREEN)
        typer.echo(f"Бот: {identity.get('name') or identity.get('username') or identity}")
    except PublishError as exc:
        typer.secho(f"Проверка не прошла: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc

    try:
        updates = await publisher.fetch_updates()
    except PublishError as exc:
        typer.secho(f"Не удалось получить updates: {exc}", fg=typer.colors.YELLOW)
        return

    # GET /chats is deprecated, so the channel id has to be picked out of the
    # update the platform emits when the bot is added.
    seen: dict[str, str] = {}
    for update in updates:
        chat = update.get("chat") or {}
        chat_id = chat.get("chat_id") or update.get("chat_id")
        if chat_id:
            seen[str(chat_id)] = str(chat.get("title") or update.get("update_type") or "")

    if seen:
        typer.echo("\nНайденные чаты и каналы:")
        for chat_id, title in seen.items():
            typer.echo(f"  chat_id={chat_id}  {title}")
        typer.echo("\nНужный id положите в .env как MAX_CHANNEL_ID.")
    else:
        typer.secho(
            "\nВ updates нет ни одного чата. Добавьте бота администратором в канал — "
            "id придёт событием bot_added (GET /chats в API отключён).",
            fg=typer.colors.YELLOW,
        )


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
    try:
        service = _service(database)
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
        await database.dispose()


@app.command("publish-log")
def publish_log(limit: int = typer.Option(15, "--limit", min=1, max=100)) -> None:
    """История публикаций, включая симулированные."""
    settings = get_settings()
    setup_logging(settings.log_level)
    asyncio.run(_publish_log(limit))


async def _publish_log(limit: int) -> None:
    database = _database()
    try:
        rows = await _service(database).recent(limit)
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
        await database.dispose()


__all__ = ["PublishMode"]
