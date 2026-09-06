"""Rendering competitor research into a Markdown dossier."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from channel_factory.competitors.analysis import ChannelProfile, NicheBenchmarks
from channel_factory.competitors.service import NicheDossier

COMPETITOR_ANALYSIS_PREFIX = "competitor-analysis"

PLATFORM_LABELS = {"TELEGRAM": "Telegram", "MAX": "MAX"}


def _int(value: Any) -> str:
    if value is None:
        return "—"
    return f"{round(float(value)):,}".replace(",", " ")


def _pct(value: Any, *, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:.{digits}f}%"


def _rank_pct(value: Any) -> str:
    """Percentile position inside the niche, where 100 is the top."""
    if value is None:
        return "—"
    return f"{float(value):.0f}"


def _num(value: Any, *, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}"


def _ratio(profile: ChannelProfile) -> str:
    value = profile.views_to_subscribers
    return "—" if value is None else f"{value:.2f}"


def scope_label(benchmarks: NicheBenchmarks) -> str:
    if benchmarks.platform is None:
        return "все платформы"
    return PLATFORM_LABELS.get(benchmarks.platform.value, benchmarks.platform.value)


def report_filename(benchmarks: NicheBenchmarks) -> str:
    """One file per niche and scope, so dossiers never overwrite each other."""
    suffix = "" if benchmarks.platform is None else f"-{benchmarks.platform.value.lower()}"
    return f"{COMPETITOR_ANALYSIS_PREFIX}-{benchmarks.slug}{suffix}.md"


def _leader_table(profiles: list[ChannelProfile]) -> list[str]:
    lines = [
        "| # | Канал | Платформа | Подписчики | ERR | Просмотры | Views/Subs | CPV | Цена | "
        "Позиция в нише |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for position, profile in enumerate(profiles, start=1):
        lines.append(
            f"| {position} | {profile.channel_name} "
            f"| {PLATFORM_LABELS.get(profile.platform, profile.platform)} "
            f"| {_int(profile.subscribers)} | {_pct(profile.err)} "
            f"| {_int(profile.predicted_views)} | {_ratio(profile)} "
            f"| {_num(profile.cpv)} | {_int(profile.campaign_price)} "
            f"| {_rank_pct(profile.subscribers_pct)} |"
        )
    if not profiles:
        lines.append("| — | нет данных | — | — | — | — | — | — | — | — |")
    return lines


def render_competitor_analysis(dossier: NicheDossier) -> str:
    """Render the dossier for one niche."""
    benchmarks = dossier.benchmarks
    lines = [
        f"# Конкуренты: {benchmarks.name} — {scope_label(benchmarks)}",
        "",
        f"Сгенерировано: {datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
        "",
        "## Что этот отчёт может и чего не может",
        "",
        "Источник — каталог каналов для рекламы. В нём есть то, как канал "
        "**продаётся рекламодателю**: размер аудитории, вовлечённость, прогноз "
        "просмотров, цена. В нём **нет постов, частоты публикаций, заголовков и "
        "форматов**, поэтому отчёт отвечает на вопрос «кто лидирует и что нужно, "
        "чтобы конкурировать», и не отвечает на вопрос «какой контент работает». "
        "Второй вопрос требует другого источника данных.",
        "",
        f"- Ниша: **{benchmarks.name}** (`{benchmarks.slug}`)",
        f"- Охват: **{scope_label(benchmarks)}**",
        f"- Каналов в выборке: **{benchmarks.channels_count}**",
        f"- Дата снимка: {benchmarks.snapshot_date or '—'}",
        "",
        "## Распределение ниши",
        "",
        "Робастные квантили, а не среднее: несколько гигантских каналов не должны "
        "определять картину ниши.",
        "",
        "| Показатель | p10 | p25 | Медиана | p75 | p90 | Максимум |",
        "|---|---:|---:|---:|---:|---:|---:|",
        f"| Подписчики | {_int(benchmarks.subscribers_p10)} | {_int(benchmarks.subscribers_p25)} "
        f"| {_int(benchmarks.subscribers_p50)} | {_int(benchmarks.subscribers_p75)} "
        f"| {_int(benchmarks.subscribers_p90)} | {_int(benchmarks.subscribers_max)} |",
        "",
        "| Показатель | p25 | Медиана | p75 |",
        "|---|---:|---:|---:|",
        f"| ERR | {_pct(benchmarks.err_p25)} | {_pct(benchmarks.err_p50)} "
        f"| {_pct(benchmarks.err_p75)} |",
        "",
        f"- Медианные просмотры: **{_int(benchmarks.views_p50)}**, "
        f"p90: {_int(benchmarks.views_p90)}",
        f"- Медианный CPV: **{_num(benchmarks.cpv_p50)}**, "
        f"медианная цена размещения: **{_int(benchmarks.price_p50)}**",
        f"- Доля топ-{benchmarks.head_size} каналов в подписчиках ниши: "
        f"**{_pct(benchmarks.head_share)}**",
        "",
        "## Что нужно, чтобы конкурировать",
        "",
        "Пороги — это наблюдаемые квантили ниши, а не прогноз: столько подписчиков "
        "имеют каналы, которые уже занимают соответствующую позицию.",
        "",
        "| Цель | Подписчиков | Комментарий |",
        "|---|---:|---|",
        f"| Войти в верхнюю половину | {_int(benchmarks.subscribers_p50)} "
        "| медиана ниши |",
        f"| Войти в верхнюю четверть | {_int(benchmarks.subscribers_p75)} | p75 |",
        f"| Войти в топ-10% | {_int(benchmarks.subscribers_p90)} | p90 |",
        f"| Уровень лидера | {_int(benchmarks.subscribers_max)} | максимум в нише |",
        "",
        f"Чтобы не выглядеть слабее среднего по вовлечённости, нужен ERR не ниже "
        f"**{_pct(benchmarks.err_p50)}** (медиана ниши), сильный уровень — "
        f"**{_pct(benchmarks.err_p75)}** (p75).",
        "",
        "## Лидеры по подписчикам",
        "",
        *_leader_table(dossier.by_subscribers),
        "",
        "## Лидеры по вовлечённости",
        "",
    ]

    if dossier.engagement_floor:
        lines += [
            f"Только каналы от **{_int(dossier.engagement_floor)}** подписчиков "
            "(медиана ниши). Без этого порога список заполняют мелкие каналы, у "
            "которых высокий ERR — следствие маленького знаменателя, а не силы канала.",
            "",
        ]
    lines += _leader_table(dossier.by_engagement)

    lines += [
        "",
        "## Самые дорогие размещения",
        "",
        "Цена — это то, что площадка запрашивает у рекламодателя: косвенный сигнал "
        "того, как канал сам оценивает свою аудиторию.",
        "",
        *_leader_table(dossier.by_price),
    ]

    if dossier.watched:
        lines += [
            "",
            "## На отслеживании",
            "",
            *_leader_table(dossier.watched),
        ]
        notes = [item for item in dossier.watched if item.watch_notes]
        if notes:
            lines += ["", "Заметки:", ""]
            lines += [f"- **{item.channel_name}**: {item.watch_notes}" for item in notes]

    lines += [
        "",
        "## Ограничения",
        "",
        "- Данные описывают рекламное предложение каналов, а не их контент.",
        "- Один снимок во времени: динамику и растущих игроков определить нельзя, "
        "для этого нужна вторая выгрузка.",
        "- «Позиция в нише» — перцентиль по подписчикам внутри своей ниши и своей "
        "платформы (100 — вершина).",
        "- Каналы, которых нет в каталоге Директа, в выборку не попадают: это срез "
        "рекламного рынка, а не всех каналов платформы.",
        "",
    ]
    return "\n".join(lines)


def write_competitor_report(directory: Path, dossier: NicheDossier) -> Path:
    """Write the dossier, returning the path written."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / report_filename(dossier.benchmarks)
    path.write_text(render_competitor_analysis(dossier), encoding="utf-8")
    return path


def format_profile_line(profile: ChannelProfile) -> str:
    """One-line channel summary for CLI output."""
    subscribers = _int(profile.subscribers)
    err = _pct(profile.err)
    position = _rank_pct(profile.subscribers_pct)
    niche = profile.niche_name or "—"
    return (
        f"{profile.channel_name[:38]:38} {profile.platform:8} "
        f"{subscribers:>10} подп.  ERR {err:>6}  поз. {position:>3}  {niche}"
    )
