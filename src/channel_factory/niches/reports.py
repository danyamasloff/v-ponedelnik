"""Rendering niche analysis into Markdown and CSV reports.

Reports are rendered from a **stored run**, never from a fresh computation, so
a report file always matches the scores in the database — even if more data has
been imported since.

Every report states what data it stands on (sources, date range, channel
counts) and which components were unavailable or left at neutral defaults. A
report must never read as settled market truth when it is not.
"""

from __future__ import annotations

import csv
import io
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from channel_factory.db.models import Niche, NicheScore, NicheScoreRun

MARKET_OVERVIEW_FILE = "market-overview.md"
NICHE_RANKING_FILE = "niche-ranking.md"
NICHE_RANKING_CSV = "niche-ranking.csv"
CROSS_PLATFORM_FILE = "cross-platform-niches.md"

PLATFORM_LABELS = {"TELEGRAM": "Telegram", "MAX": "MAX"}

# Component order in reports: data-derived first, subjective after, so a reader
# sees what the market said before what we assumed.
COMPONENT_ORDER = (
    "audience_value",
    "ad_budget_level",
    "engagement",
    "reach_efficiency",
    "market_size",
    "low_competition",
    "growth_potential",
    "content_scalability",
    "low_production_cost",
    "originality_potential",
    "brand_safety",
    "low_monetization_risk",
)


@dataclass(frozen=True)
class ReportRow:
    """One niche as it appears in a report."""

    rank: int | None
    slug: str
    name: str
    score: float | None
    channels_count: int
    insufficient_data: bool
    components: dict[str, Any]
    metrics: dict[str, Any]

    def component_value(self, key: str) -> float | None:
        component = self.components.get(key) or {}
        return component.get("value")

    @property
    def default_rating_keys(self) -> tuple[str, ...]:
        return tuple(
            key
            for key, component in self.components.items()
            if component.get("is_default_rating")
        )

    @property
    def unavailable_keys(self) -> tuple[str, ...]:
        return tuple(
            key for key, component in self.components.items() if not component.get("available")
        )


def build_rows(scores: list[tuple[NicheScore, Niche]]) -> list[ReportRow]:
    """Turn stored rows into report rows."""
    return [
        ReportRow(
            rank=score.rank,
            slug=niche.slug,
            name=niche.name,
            score=None if score.score is None else float(score.score),
            channels_count=score.channels_count,
            insufficient_data=score.insufficient_data,
            components=score.components or {},
            metrics=score.metrics or {},
        )
        for score, niche in scores
    ]


def _int(value: Any) -> str:
    if value is None:
        return "—"
    return f"{round(float(value)):,}".replace(",", " ")


def _pct(value: Any, *, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{float(value) * 100:.{digits}f}%"


def _num(value: Any, *, digits: int = 2) -> str:
    if value is None:
        return "—"
    return f"{float(value):.{digits}f}"


def _score(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


def scope_label(run: NicheScoreRun) -> str:
    """Human-readable scope of a run."""
    if run.platform is None:
        return "все платформы"
    return PLATFORM_LABELS.get(run.platform.value, run.platform.value)


def report_suffix(run: NicheScoreRun) -> str:
    """Filename suffix so platform reports never overwrite the overall ones."""
    return "" if run.platform is None else f"-{run.platform.value.lower()}"


def _dataset_header(run: NicheScoreRun) -> list[str]:
    data = run.dataset or {}
    sources = data.get("sources") or []
    platforms = data.get("platforms") or {}
    lines = [
        f"- Прогон: `{run.id}` от {run.created_at:%Y-%m-%d %H:%M} UTC",
        f"- Версия формулы: `{run.score_version}`",
        f"- Охват: **{scope_label(run)}**",
        f"- Источники данных: {', '.join(sources) if sources else '—'}",
        f"- Каналов в базе: {data.get('channels', 0)} "
        f"(без категории: {data.get('channels_without_niche', 0)})",
        f"- Снимков метрик: {data.get('snapshots', 0)} "
        f"на {data.get('snapshot_dates', 0)} дат(ы)",
        f"- Период данных: {data.get('first_date') or '—'} — {data.get('last_date') or '—'}",
        "- Платформы: "
        + (", ".join(f"{name}: {count}" for name, count in platforms.items()) or "—"),
    ]
    if run.as_of_date:
        lines.append(f"- Расчёт на дату: {run.as_of_date}")
    return lines


def _caveats(run: NicheScoreRun, rows: list[ReportRow]) -> list[str]:
    """Everything a reader must know before trusting the numbers."""
    notes: list[str] = []
    scored = [row for row in rows if row.score is not None]

    if run.low_confidence:
        notes.append(
            f"**Мало ниш для ранжирования** ({len(scored)}). Перцентильные ранги "
            "на такой выборке грубые: разница в несколько пунктов между нишами "
            "ничего не значит."
        )

    defaults_only = [
        row.name
        for row in scored
        if len(row.default_rating_keys) == len(
            [k for k, c in row.components.items() if c.get("source") == "manual"]
        )
    ]
    if defaults_only:
        notes.append(
            f"**Субъективные оценки не заданы** для {len(defaults_only)} из {len(scored)} ниш "
            "— используются нейтральные значения (50), то есть эти компоненты сейчас "
            "не различают ниши. Заполните `manual_overrides` в `config/niche_score.yaml`."
        )

    unavailable: dict[str, int] = {}
    for row in scored:
        for key in row.unavailable_keys:
            unavailable[key] = unavailable.get(key, 0) + 1
    if unavailable:
        listed = ", ".join(f"`{key}` ({count} ниш)" for key, count in sorted(unavailable.items()))
        notes.append(
            f"**Недоступные компоненты**: {listed}. Их веса перераспределены между "
            "доступными компонентами, значения не додумывались."
        )

    skipped = [row for row in rows if row.insufficient_data]
    if skipped:
        notes.append(
            f"**Исключены из ранжирования** (мало каналов): {len(skipped)} ниш — "
            + ", ".join(f"{row.name} ({row.channels_count})" for row in skipped[:10])
        )

    notes.append(
        "Niche Score — инструмент сравнения, а не приговор. Он показывает, какие "
        "ниши стоит изучить внимательнее, и обязан читаться вместе с разбивкой "
        "по компонентам."
    )
    return notes


def render_market_overview(run: NicheScoreRun, rows: list[ReportRow]) -> str:
    """Market statistics per niche, without any scoring."""
    lines = [
        f"# Обзор рынка — {scope_label(run)}",
        "",
        f"Сгенерировано: {datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
        "",
        "## Данные",
        "",
        *_dataset_header(run),
        "",
        "## Метрики по нишам",
        "",
        "Все показатели — робастные (медиана и квартили), среднее не используется: "
        "один гигантский канал не должен определять нишу.",
        "",
        "| Ниша | Каналов | Подписчики (медиана) | p25 | p75 | ERR | Просмотры | "
        "Views/Subs | CPV | Размещение | Топ-3 доля |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in sorted(rows, key=lambda r: r.metrics.get("channels_count", 0), reverse=True):
        metrics = row.metrics
        lines.append(
            f"| {row.name} | {row.channels_count} "
            f"| {_int(metrics.get('median_subscribers'))} "
            f"| {_int(metrics.get('p25_subscribers'))} "
            f"| {_int(metrics.get('p75_subscribers'))} "
            f"| {_pct(metrics.get('median_err'))} "
            f"| {_int(metrics.get('median_views'))} "
            f"| {_num(metrics.get('median_views_to_subs'))} "
            f"| {_num(metrics.get('median_cpv'))} "
            f"| {_int(metrics.get('median_campaign_price'))} "
            f"| {_pct(metrics.get('top_channel_share'))} |"
        )

    lines += [
        "",
        "Денежные значения приведены в валюте источника, без конвертации.",
        "",
    ]
    return "\n".join(lines)


def render_niche_ranking(run: NicheScoreRun, rows: list[ReportRow]) -> str:
    """Ranked niches with the full component breakdown."""
    scored = [row for row in rows if row.score is not None]
    lines = [
        f"# Ранжирование ниш — {scope_label(run)}",
        "",
        f"Сгенерировано: {datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
        "",
        "## Данные",
        "",
        *_dataset_header(run),
        "",
        "## Важно",
        "",
    ]
    lines += [f"- {note}" for note in _caveats(run, rows)]
    lines += [
        "",
        "## Рейтинг",
        "",
        "| # | Ниша | Score | Каналов |",
        "|---:|---|---:|---:|",
    ]
    for row in scored:
        lines.append(
            f"| {row.rank} | {row.name} | **{_score(row.score)}** | {row.channels_count} |"
        )

    if not scored:
        lines.append("| — | нет ниш с достаточным количеством данных | — | — |")

    lines += [
        "",
        "## Разбивка по компонентам",
        "",
        "Каждый компонент приведён к шкале 0–100, где больше — всегда лучше "
        "(«плохие» факторы инвертированы). `—` означает, что компонент недоступен "
        "для этой ниши и его вес перераспределён.",
        "",
    ]

    keys = [key for key in COMPONENT_ORDER if any(key in row.components for row in scored)]
    header = "| Ниша | " + " | ".join(
        (scored[0].components.get(key, {}).get("label", key) if scored else key) for key in keys
    )
    lines.append(header + " |")
    lines.append("|---" * (len(keys) + 1) + "|")
    for row in scored:
        cells = [
            "—"
            if row.component_value(key) is None
            else f"{row.component_value(key):.0f}"
            for key in keys
        ]
        lines.append(f"| {row.name} | " + " | ".join(cells) + " |")

    weights = (run.config or {}).get("weights", {})
    if weights:
        lines += [
            "",
            "## Веса компонентов",
            "",
            "| Компонент | Вес |",
            "|---|---:|",
        ]
        for key in COMPONENT_ORDER:
            if key in weights:
                lines.append(f"| {key} | {weights[key]:.2f} |")

    lines.append("")
    return "\n".join(lines)


def render_ranking_csv(run: NicheScoreRun, rows: list[ReportRow]) -> str:
    """Machine-readable ranking for spreadsheets and further analysis."""
    buffer = io.StringIO()
    keys = list(COMPONENT_ORDER)
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(
        [
            "rank",
            "slug",
            "name",
            "score",
            "score_version",
            "run_id",
            "channels_count",
            "insufficient_data",
            "median_subscribers",
            "median_err",
            "median_views",
            "median_cpv",
            "median_campaign_price",
            "median_views_to_subs",
            "top_channel_share",
            "median_growth_rate",
            *keys,
        ]
    )
    for row in rows:
        metrics = row.metrics
        writer.writerow(
            [
                row.rank if row.rank is not None else "",
                row.slug,
                row.name,
                "" if row.score is None else f"{row.score:.2f}",
                run.score_version,
                run.id,
                row.channels_count,
                "true" if row.insufficient_data else "false",
                metrics.get("median_subscribers", ""),
                metrics.get("median_err", ""),
                metrics.get("median_views", ""),
                metrics.get("median_cpv", ""),
                metrics.get("median_campaign_price", ""),
                metrics.get("median_views_to_subs", ""),
                metrics.get("top_channel_share", ""),
                metrics.get("median_growth_rate", ""),
                *[
                    ""
                    if row.component_value(key) is None
                    else f"{row.component_value(key):.2f}"
                    for key in keys
                ],
            ]
        )
    return buffer.getvalue()


def write_reports(directory: Path, run: NicheScoreRun, rows: list[ReportRow]) -> list[Path]:
    """Write all report files, returning the paths written.

    Platform-scoped runs get a filename suffix so a Telegram ranking never
    overwrites the overall one.
    """
    directory.mkdir(parents=True, exist_ok=True)
    suffix = report_suffix(run)

    def named(filename: str) -> str:
        stem, _, extension = filename.rpartition(".")
        return f"{stem}{suffix}.{extension}"

    files = {
        named(MARKET_OVERVIEW_FILE): render_market_overview(run, rows),
        named(NICHE_RANKING_FILE): render_niche_ranking(run, rows),
        named(NICHE_RANKING_CSV): render_ranking_csv(run, rows),
    }
    written: list[Path] = []
    for name, content in files.items():
        path = directory / name
        path.write_text(content, encoding="utf-8")
        written.append(path)
    return written
