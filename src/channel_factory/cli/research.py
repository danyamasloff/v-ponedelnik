"""Autonomous research engine commands."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import typer
from sqlalchemy import func, select

from channel_factory.cli.app import app
from channel_factory.core.config import get_settings
from channel_factory.core.enums import ClusterStatus
from channel_factory.core.logging import setup_logging
from channel_factory.db.models import ResearchCluster, ResearchItem, ResearchSource
from channel_factory.db.repositories.research import (
    ResearchClusterRepository,
    ResearchItemRepository,
    ResearchSourceRepository,
)
from channel_factory.db.session import Database
from channel_factory.research.config import (
    ResearchConfigError,
    load_source_configs,
    load_topic_score_config,
)
from channel_factory.research.factory import (
    build_judge_client,
    build_llm_client,
    build_research_service,
)
from channel_factory.research.judge import ClusterJudge
from channel_factory.research.reports import render_digest, write_digest

NO_DATA = "NO DATA: пока ничего не собрано. Запустите research-poll."


def _database() -> Database:
    return Database(get_settings().database_url)


def _load_sources():
    settings = get_settings()
    try:
        return load_source_configs(settings.research_sources_config)
    except ResearchConfigError as exc:
        typer.secho(f"Ошибка конфигурации источников: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc


def _load_score_config():
    settings = get_settings()
    try:
        return load_topic_score_config(settings.topic_score_config)
    except ResearchConfigError as exc:
        typer.secho(f"Ошибка конфигурации Topic Score: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc


@app.command("research-sync-sources")
def research_sync_sources() -> None:
    """Синхронизировать whitelist источников из конфига в базу."""
    settings = get_settings()
    setup_logging(settings.log_level)
    configs = _load_sources()
    asyncio.run(_sync(configs))


async def _sync(configs) -> None:
    database = _database()
    try:
        service = build_research_service(database, get_settings())
        created, updated = await service.sync_sources(configs)
        typer.secho(
            f"Источники синхронизированы: {created} добавлено, {updated} обновлено.",
            fg=typer.colors.GREEN,
        )
    finally:
        await database.dispose()


@app.command("research-sources")
def research_sources() -> None:
    """Показать зарегистрированные источники и их состояние."""
    settings = get_settings()
    setup_logging(settings.log_level)
    asyncio.run(_list_sources())


async def _list_sources() -> None:
    database = _database()
    try:
        async with database.session() as session:
            sources = await ResearchSourceRepository(session).all()
        if not sources:
            typer.secho(
                "Источники не зарегистрированы. Запустите research-sync-sources.",
                fg=typer.colors.YELLOW,
            )
            return
        header = f"{'KEY':26} {'PROVIDER':17} {'TRUST':20} {'ON':3} {'FAIL':>4}  ПОСЛЕДНИЙ ОПРОС"
        typer.echo(header)
        typer.echo("-" * len(header))
        for source in sources:
            polled = (
                f"{source.last_polled_at:%Y-%m-%d %H:%M}" if source.last_polled_at else "никогда"
            )
            flag = "да" if source.enabled else "нет"
            line = (
                f"{source.key:26} {source.provider.value:17} {source.trust.value:20} "
                f"{flag:3} {source.consecutive_failures:>4}  {polled}"
            )
            colour = typer.colors.RED if source.consecutive_failures else None
            typer.secho(line, fg=colour)
            if source.last_error:
                typer.secho(f"{'':26} ! {source.last_error[:110]}", fg=typer.colors.YELLOW)
    finally:
        await database.dispose()


@app.command("research-poll")
def research_poll(
    source: list[str] | None = typer.Option(None, "--source", help="Опросить только эти ключи"),
    force: bool = typer.Option(False, "--force", help="Игнорировать интервал опроса"),
    limit: int = typer.Option(
        100, "--limit", min=1, max=500, help="Максимум элементов с источника"
    ),
) -> None:
    """Опросить источники и сохранить новые публикации."""
    settings = get_settings()
    setup_logging(settings.log_level)
    asyncio.run(_poll(list(source) if source else None, force, limit))


async def _poll(only: list[str] | None, force: bool, limit: int) -> None:
    database = _database()
    try:
        service = build_research_service(database, get_settings())
        outcome = await service.poll(only=only, force=force, limit_per_source=limit)
        typer.echo(f"Прогон      : {outcome.run_id}")
        typer.echo(
            f"Источники   : {outcome.sources_polled} опрошено, "
            f"{outcome.sources_skipped} пропущено (не пришло время), "
            f"{outcome.sources_failed} с ошибкой"
        )
        typer.echo(
            f"Элементы    : {outcome.items_new} новых, {outcome.items_updated} обновлено, "
            f"{outcome.items_unchanged} без изменений"
        )
        typer.echo(f"Длительность: {outcome.duration_ms} ms")
        for error in outcome.errors:
            typer.secho(f"  ! {error}", fg=typer.colors.YELLOW)
    finally:
        await database.dispose()


@app.command("research-cluster")
def research_cluster(
    limit: int = typer.Option(500, "--limit", min=1, max=5000),
) -> None:
    """Сгруппировать новые публикации в информационные события."""
    settings = get_settings()
    setup_logging(settings.log_level)
    asyncio.run(_cluster(limit))


async def _cluster(limit: int) -> None:
    database = _database()
    try:
        service = build_research_service(database, get_settings())
        outcome = await service.cluster(limit=limit)
        typer.echo(f"Прогон     : {outcome.run_id}")
        typer.echo(f"Обработано : {outcome.items_processed} элементов")
        typer.echo(
            f"Кластеры   : {outcome.clusters_created} создано, "
            f"{outcome.items_attached} привязок"
        )
        if outcome.by_method:
            rendered = ", ".join(
                f"{key}={value}" for key, value in sorted(outcome.by_method.items())
            )
            typer.echo(f"Совпадения : {rendered}")
        typer.echo(f"Длительность: {outcome.duration_ms} ms")
    finally:
        await database.dispose()


@app.command("score-topics")
def score_topics(
    limit: int = typer.Option(200, "--limit", min=1, max=2000),
    use_llm: bool = typer.Option(
        True, "--llm/--no-llm", help="Использовать LLM для субъективных компонентов"
    ),
) -> None:
    """Посчитать Topic Score и распределить кластеры по SELECTED/BACKLOG/FILTERED."""
    settings = get_settings()
    setup_logging(settings.log_level)
    asyncio.run(_score(limit, use_llm))


async def _score(limit: int, use_llm: bool) -> None:
    settings = get_settings()
    database = _database()
    try:
        service = build_research_service(database, settings)
        judge = None
        judge_label = "не используется"
        if use_llm:
            judge_client, judge_label = build_judge_client(database, settings)
            judge = ClusterJudge(judge_client)
        outcome = await service.score(limit=limit, judge=judge)

        typer.echo(f"Прогон     : {outcome.run_id}")
        typer.echo(f"Судья      : {judge_label}")
        typer.echo(f"Оценено    : {outcome.clusters_scored} кластеров")
        typer.echo(
            f"Решения    : SELECTED {outcome.selected}, BACKLOG {outcome.backlog}, "
            f"FILTERED {outcome.filtered}, EXPIRED {outcome.expired}"
        )
        if outcome.unavailable_components:
            rendered = ", ".join(
                f"{key} ({count})" for key, count in sorted(outcome.unavailable_components.items())
            )
            typer.echo(f"Недоступно : {rendered} — веса перенормированы")
        if outcome.lexicon_used:
            typer.echo(
                "Лексикон   : relevance и practical_value посчитаны детерминированно "
                "(бесплатно, эвристика)"
            )
        if judge is not None and judge.throttled_count:
            typer.secho(
                f"Троттлинг  : {judge.throttled_count} кластеров пропущены провайдером "
                "по частоте; они остаются на пересчёт",
                fg=typer.colors.YELLOW,
            )
        if judge is not None and judge.disabled_reason:
            typer.secho(f"LLM отключён: {judge.disabled_reason}", fg=typer.colors.YELLOW)
        elif not use_llm:
            typer.secho(
                "LLM не использовался (--no-llm): работают лексикон и детерминированные "
                "компоненты.",
                fg=typer.colors.YELLOW,
            )
        typer.echo(f"Длительность: {outcome.duration_ms} ms")
    finally:
        await database.dispose()


@app.command("show-clusters")
def show_clusters(
    status: str | None = typer.Option(None, "--status", help="SELECTED / BACKLOG / FILTERED ..."),
    limit: int = typer.Option(20, "--limit", min=1, max=200),
) -> None:
    """Показать кластеры, отсортированные по Topic Score."""
    settings = get_settings()
    setup_logging(settings.log_level)
    statuses = None
    if status:
        try:
            statuses = [ClusterStatus(status.upper())]
        except ValueError as exc:
            typer.secho(f"Неизвестный статус: {status}", fg=typer.colors.RED)
            raise typer.Exit(code=2) from exc
    asyncio.run(_show_clusters(statuses, limit))


async def _show_clusters(statuses, limit: int) -> None:
    database = _database()
    try:
        async with database.session() as session:
            clusters = await ResearchClusterRepository(session).top(
                statuses=statuses, limit=limit
            )
        if not clusters:
            typer.secho(NO_DATA, fg=typer.colors.YELLOW)
            return
        typer.echo(f"{'SCORE':>6} {'STATUS':9} {'ИСТ':>4} {'ДОВЕРИЕ':20} ТЕМА")
        typer.echo("-" * 100)
        for cluster in clusters:
            score = "—" if cluster.topic_score is None else f"{float(cluster.topic_score):.1f}"
            typer.echo(
                f"{score:>6} {cluster.status.value:9} {cluster.item_count:>4} "
                f"{cluster.max_trust.value:20} {cluster.canonical_title[:60]}"
            )
            typer.echo(f"{'':6} {cluster.id}")
    finally:
        await database.dispose()


@app.command("show-cluster")
def show_cluster(cluster_id: str = typer.Argument(..., help="UUID кластера")) -> None:
    """Показать, почему кластер получил такую оценку."""
    settings = get_settings()
    setup_logging(settings.log_level)
    try:
        parsed = uuid.UUID(cluster_id)
    except ValueError as exc:
        typer.secho(f"Не UUID: {cluster_id}", fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc
    asyncio.run(_show_cluster(parsed))


async def _show_cluster(cluster_id: uuid.UUID) -> None:
    database = _database()
    try:
        async with database.session() as session:
            repo = ResearchClusterRepository(session)
            cluster = await repo.by_id(cluster_id)
            if cluster is None:
                typer.secho(f"Кластер {cluster_id} не найден.", fg=typer.colors.RED)
                raise typer.Exit(code=1)
            score = await repo.latest_score(cluster_id)
            sources = await repo.source_breakdown(cluster_id)

        typer.echo(f"Тема      : {cluster.canonical_title}")
        typer.echo(f"Ключ      : {cluster.cluster_key}")
        typer.echo(f"Событие   : {cluster.event_type.value}")
        typer.echo(
            f"Сущности  : вендор={cluster.vendor or '—'} продукт={cluster.product or '—'} "
            f"версия={cluster.version or '—'}"
        )
        typer.echo(f"Статус    : {cluster.status.value}")
        typer.echo(
            f"Score     : "
            f"{'—' if cluster.topic_score is None else f'{float(cluster.topic_score):.1f}'} "
            f"({cluster.score_version or '—'})"
        )
        typer.echo(f"Источников: {cluster.item_count}, максимум доверия {cluster.max_trust.value}")

        if score is not None and score.components:
            typer.echo("\nКомпоненты:")
            typer.echo(f"  {'КОМПОНЕНТ':22} {'ТИП':5} {'ВЕС':>5} {'ЗНАЧ':>6}  ПРИМЕЧАНИЕ")
            for key, payload in score.components.items():
                value = payload.get("value")
                rendered = "—" if value is None else f"{float(value):.0f}"
                typer.echo(
                    f"  {key:22} {payload.get('source', '?'):5} "
                    f"{float(payload.get('weight', 0)):>5.2f} {rendered:>6}  "
                    f"{payload.get('note') or ''}"
                )

        typer.echo("\nИсточники:")
        for entry in sources:
            typer.echo(
                f"  [{entry['trust']:20}] {entry['role']:10} matched_by={entry['matched_by']:11} "
                f"{entry['source']}"
            )
            typer.echo(f"    {entry['url']}")
    finally:
        await database.dispose()


@app.command("research-status")
def research_status() -> None:
    """Сводка по движку исследований."""
    settings = get_settings()
    setup_logging(settings.log_level)
    asyncio.run(_status())


async def _status() -> None:
    settings = get_settings()
    database = _database()
    try:
        async with database.session() as session:
            sources_total = (
                await session.execute(select(func.count()).select_from(ResearchSource))
            ).scalar_one()
            sources_on = (
                await session.execute(
                    select(func.count())
                    .select_from(ResearchSource)
                    .where(ResearchSource.enabled.is_(True))
                )
            ).scalar_one()
            items_total = (
                await session.execute(select(func.count()).select_from(ResearchItem))
            ).scalar_one()
            clusters_total = (
                await session.execute(select(func.count()).select_from(ResearchCluster))
            ).scalar_one()
            item_counts = await ResearchItemRepository(session).counts_by_status()
            cluster_counts = await ResearchClusterRepository(session).counts_by_status()

        typer.echo(f"Источники : {sources_on} активных из {sources_total}")
        typer.echo(f"Элементы  : {items_total}")
        if item_counts:
            typer.echo(
                "            "
                + ", ".join(f"{key.value}={value}" for key, value in sorted(item_counts.items()))
            )
        typer.echo(f"Кластеры  : {clusters_total}")
        if cluster_counts:
            typer.echo(
                "            "
                + ", ".join(
                    f"{key.value}={value}" for key, value in sorted(cluster_counts.items())
                )
            )
        if not items_total:
            typer.secho(f"\n{NO_DATA}", fg=typer.colors.YELLOW)

        await _print_cost(database, settings)
    finally:
        await database.dispose()


async def _print_cost(database: Database, settings) -> None:
    llm = build_llm_client(database, settings)
    now = datetime.now(UTC)
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    today = await llm.spend(since=day_start)
    month = await llm.spend(since=day_start.replace(day=1))
    typer.echo(
        f"\nРасходы LLM: сегодня ${today:.4f} из ${settings.daily_cost_limit_usd:.2f}, "
        f"за месяц ${month:.4f} из ${settings.monthly_cost_limit_usd:.2f}"
    )
    if today >= settings.daily_cost_limit_usd or month >= settings.monthly_cost_limit_usd:
        typer.secho(
            "COST_LIMIT_REACHED: новые LLM-вызовы блокируются, сбор источников продолжается.",
            fg=typer.colors.RED,
        )
    if settings.gemini_api_key:
        from channel_factory.providers.llm.gemini import GeminiClient

        gemini = GeminiClient(
            database,
            api_key=settings.gemini_api_key,
            daily_request_limit=settings.gemini_daily_request_limit,
        )
        used = await gemini.requests_today()
        typer.echo(
            f"Gemini free tier: {used} из {settings.gemini_daily_request_limit} "
            "запросов сегодня (стоимость $0)"
        )
    elif not settings.anthropic_api_key:
        typer.secho(
            "Ключей LLM нет: relevance и practical_value считаются лексиконом, "
            "audience_fit и content_potential недоступны — веса перенормируются.",
            fg=typer.colors.YELLOW,
        )


@app.command("ai-cost")
def ai_cost() -> None:
    """Показать расходы на LLM и лимиты."""
    settings = get_settings()
    setup_logging(settings.log_level)

    async def run() -> None:
        database = _database()
        try:
            await _print_cost(database, settings)
        finally:
            await database.dispose()

    asyncio.run(run())


@app.command("research-report")
def research_report(
    limit: int = typer.Option(15, "--limit", min=1, max=100),
    output: Path | None = typer.Option(None, "--output", help="Каталог для отчёта"),
) -> None:
    """Сгенерировать research digest из сохранённых данных."""
    settings = get_settings()
    setup_logging(settings.log_level)
    asyncio.run(_report(limit, output or settings.reports_dir))


async def _report(limit: int, directory: Path) -> None:
    settings = get_settings()
    database = _database()
    try:
        score_config = _load_score_config()
        async with database.session() as session:
            repo = ResearchClusterRepository(session)
            selected = await repo.top(statuses=[ClusterStatus.SELECTED], limit=limit)
            payload = []
            for cluster in selected:
                score = await repo.latest_score(cluster.id)
                payload.append(
                    (
                        cluster,
                        score.components if score else {},
                        await repo.source_breakdown(cluster.id),
                    )
                )

            cluster_counts = await repo.counts_by_status()
            sources_total = (
                await session.execute(select(func.count()).select_from(ResearchSource))
            ).scalar_one()
            sources_on = (
                await session.execute(
                    select(func.count())
                    .select_from(ResearchSource)
                    .where(ResearchSource.enabled.is_(True))
                )
            ).scalar_one()
            items_total = (
                await session.execute(select(func.count()).select_from(ResearchItem))
            ).scalar_one()

        if not items_total:
            typer.secho(NO_DATA, fg=typer.colors.YELLOW)
            return

        llm = build_llm_client(database, settings)
        now = datetime.now(UTC)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        cost = {
            "today": await llm.spend(since=day_start),
            "month": await llm.spend(since=day_start.replace(day=1)),
            "daily_limit": Decimal(settings.daily_cost_limit_usd),
            "monthly_limit": Decimal(settings.monthly_cost_limit_usd),
        }

        content = render_digest(
            clusters=payload,
            counters={
                "sources_total": sources_total,
                "sources_enabled": sources_on,
                "items_total": items_total,
                "clusters_total": sum(cluster_counts.values()),
                "selected": cluster_counts.get(ClusterStatus.SELECTED, 0),
                "backlog": cluster_counts.get(ClusterStatus.BACKLOG, 0),
                "filtered": cluster_counts.get(ClusterStatus.FILTERED, 0),
            },
            cost=cost,
            llm_available=bool(settings.gemini_api_key or settings.anthropic_api_key),
            llm_disabled_reason=(
                None
                if (settings.gemini_api_key or settings.anthropic_api_key)
                else "ключей LLM нет; relevance и practical_value посчитаны лексиконом"
            ),
            score_version=score_config.score_version,
        )
        path = write_digest(directory, content)
        typer.echo(f"Отчёт: {path}")
    finally:
        await database.dispose()
