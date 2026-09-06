"""Research digest report.

Rendered from what is stored, never from a fresh computation, so the file and
the database always agree. Every selected topic carries the reason it was
selected: sources, trust, component breakdown and which components were
unavailable.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from channel_factory.db.models import ResearchCluster

RESEARCH_DIGEST_FILE = "research-digest.md"


def _score(value: Any) -> str:
    return "—" if value is None else f"{float(value):.1f}"


def _component_row(key: str, payload: dict[str, Any]) -> str:
    value = payload.get("value")
    rendered = "—" if value is None else f"{float(value):.0f}"
    source = payload.get("source", "?")
    weight = payload.get("weight", 0)
    note = payload.get("note") or ""
    return f"| {key} | {source} | {float(weight):.2f} | {rendered} | {note} |"


def render_digest(
    *,
    clusters: list[tuple[ResearchCluster, dict[str, Any], list[dict[str, Any]]]],
    counters: dict[str, int],
    cost: dict[str, Decimal],
    llm_available: bool,
    llm_disabled_reason: str | None = None,
    score_version: str = "topic_score_v1",
) -> str:
    """Render the digest from stored clusters, their scores and their sources."""
    lines = [
        "# Research digest",
        "",
        f"Сгенерировано: {datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
        "",
        "## Состояние",
        "",
        f"- Версия формулы: `{score_version}`",
        f"- Источников активно: {counters.get('sources_enabled', 0)} "
        f"из {counters.get('sources_total', 0)}",
        f"- Элементов всего: {counters.get('items_total', 0)}",
        f"- Кластеров всего: {counters.get('clusters_total', 0)}",
        f"- Отобрано (SELECTED): {counters.get('selected', 0)}, "
        f"в бэклоге: {counters.get('backlog', 0)}, "
        f"отфильтровано: {counters.get('filtered', 0)}",
        "",
        "## Расходы на LLM",
        "",
        f"- Сегодня: ${cost.get('today', Decimal(0)):.4f} "
        f"из ${cost.get('daily_limit', Decimal(0)):.2f}",
        f"- За месяц: ${cost.get('month', Decimal(0)):.4f} "
        f"из ${cost.get('monthly_limit', Decimal(0)):.2f}",
        "",
    ]

    if not llm_available:
        lines += [
            "> ⚠️ **LLM не использовался**"
            f"{f': {llm_disabled_reason}' if llm_disabled_reason else ''}."
            " `relevance` и `practical_value` посчитаны лексиконом — это эвристика"
            " по словам, а не суждение модели; в разбивке видно, какие термины"
            " сработали. `audience_fit` и `content_potential` недоступны, их веса"
            " перенормированы. Рабочий режим, но ранжирование грубее.",
            "",
        ]

    lines += ["## Отобранные темы", ""]
    if not clusters:
        lines += ["Нет отобранных тем.", ""]
    else:
        lines += ["| # | Score | Источников | Доверие | Тема |", "|---:|---:|---:|---|---|"]
        for position, (cluster, _, _) in enumerate(clusters, start=1):
            lines.append(
                f"| {position} | **{_score(cluster.topic_score)}** | {cluster.item_count} "
                f"| {cluster.max_trust.value} | {cluster.canonical_title} |"
            )
        lines.append("")

        for position, (cluster, score_payload, sources) in enumerate(clusters, start=1):
            lines += [
                f"### {position}. {cluster.canonical_title}",
                "",
                f"- Кластер: `{cluster.cluster_key}`",
                f"- Тип события: {cluster.event_type.value}",
                f"- Вендор: {cluster.vendor or '—'}, продукт: {cluster.product or '—'}, "
                f"версия: {cluster.version or '—'}",
                f"- Score: **{_score(cluster.topic_score)}** ({cluster.status.value})",
                "",
                "| Компонент | Источник | Вес | Значение | Примечание |",
                "|---|---|---:|---:|---|",
            ]
            for key, payload in (score_payload or {}).items():
                lines.append(_component_row(key, payload))
            lines += ["", "**Источники:**", ""]
            for entry in sources:
                lines.append(
                    f"- [{entry['trust']}] {entry['source']} — "
                    f"[{entry['title'][:90]}]({entry['url']}) "
                    f"({entry['role']}, matched_by={entry['matched_by']})"
                )
            lines.append("")

    lines += [
        "## Как читать",
        "",
        "- Компонент со значением `—` недоступен для этого кластера; его вес "
        "перераспределён между остальными, значение не додумывалось.",
        "- `matched_by` показывает, каким слоем дедупликации источник привязан "
        "к событию: URL, FINGERPRINT, ENTITY, SIMHASH или SEED.",
        "- Низкий score никогда не означает окончательного отказа: кластер "
        "остаётся в FILTERED и пересчитывается, когда появляются новые источники.",
        "",
    ]
    return "\n".join(lines)


def write_digest(directory: Path, content: str) -> Path:
    """Write the digest, returning the path written."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / RESEARCH_DIGEST_FILE
    path.write_text(content, encoding="utf-8")
    return path
