"""Comparing niche rankings across Telegram and MAX.

The brand is launched on both platforms at once, so the question is not "where
is this niche best" but "where is it good enough on *both*". A niche that wins
on Telegram and collapses on MAX is not a dual-platform niche.

The ranking criterion is therefore the **weaker** of the two scores: a
dual-platform brand is limited by its weaker platform, exactly like a chain by
its weakest link. The gap between the platforms is reported alongside, because a
large gap is itself a finding.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from channel_factory.db.models import NicheScoreRun
from channel_factory.niches.reports import CROSS_PLATFORM_FILE, ReportRow

# Above this difference in score the niche behaves differently on the two
# platforms and should not be treated as one dual-platform opportunity.
ASYMMETRY_THRESHOLD = 15.0


@dataclass(frozen=True)
class CrossPlatformNiche:
    """One niche as seen from both platform rankings."""

    slug: str
    name: str
    score_telegram: float | None = None
    rank_telegram: int | None = None
    channels_telegram: int = 0
    score_max: float | None = None
    rank_max: int | None = None
    channels_max: int = 0

    @property
    def on_both(self) -> bool:
        return self.score_telegram is not None and self.score_max is not None

    @property
    def weakest_score(self) -> float | None:
        """The limiting score for a brand present on both platforms."""
        if not self.on_both:
            return None
        return min(self.score_telegram, self.score_max)  # type: ignore[type-var]

    @property
    def gap(self) -> float | None:
        if not self.on_both:
            return None
        return abs(self.score_telegram - self.score_max)  # type: ignore[operator]

    @property
    def is_asymmetric(self) -> bool:
        gap = self.gap
        return gap is not None and gap >= ASYMMETRY_THRESHOLD


def compare_platforms(
    telegram_rows: list[ReportRow], max_rows: list[ReportRow]
) -> list[CrossPlatformNiche]:
    """Join two platform rankings by niche, strongest weakest-link first."""
    telegram = {row.slug: row for row in telegram_rows}
    maximum = {row.slug: row for row in max_rows}

    niches: list[CrossPlatformNiche] = []
    for slug in sorted(set(telegram) | set(maximum)):
        tg = telegram.get(slug)
        mx = maximum.get(slug)
        niches.append(
            CrossPlatformNiche(
                slug=slug,
                name=(tg or mx).name,  # type: ignore[union-attr]
                score_telegram=tg.score if tg else None,
                rank_telegram=tg.rank if tg else None,
                channels_telegram=tg.channels_count if tg else 0,
                score_max=mx.score if mx else None,
                rank_max=mx.rank if mx else None,
                channels_max=mx.channels_count if mx else 0,
            )
        )

    return sorted(
        niches,
        key=lambda niche: (niche.on_both, niche.weakest_score or -1.0),
        reverse=True,
    )


def _fmt(value: float | None, *, digits: int = 1) -> str:
    return "—" if value is None else f"{value:.{digits}f}"


def render_cross_platform(
    telegram_run: NicheScoreRun,
    max_run: NicheScoreRun,
    niches: list[CrossPlatformNiche],
) -> str:
    """Render the dual-platform comparison."""
    both = [niche for niche in niches if niche.on_both]
    single = [niche for niche in niches if not niche.on_both]

    lines = [
        "# Ниши на обеих платформах: Telegram + MAX",
        "",
        f"Сгенерировано: {datetime.now(UTC):%Y-%m-%d %H:%M} UTC",
        "",
        "## Как читать",
        "",
        "Бренд запускается в Telegram и MAX одновременно, поэтому ниша оценивается "
        "по **слабейшей** из двух платформ: связка не может быть сильнее своего "
        "слабого звена. Столбец «разрыв» показывает, насколько по-разному ниша "
        "выглядит на платформах — большой разрыв означает, что это фактически две "
        "разные возможности, а не одна.",
        "",
        f"- Прогон Telegram: `{telegram_run.id}` "
        f"({telegram_run.created_at:%Y-%m-%d %H:%M} UTC)",
        f"- Прогон MAX: `{max_run.id}` ({max_run.created_at:%Y-%m-%d %H:%M} UTC)",
        f"- Версия формулы: `{telegram_run.score_version}`",
        "",
        "Оценки каждой платформы посчитаны **внутри** этой платформы: перцентиль "
        "Telegram сравнивает ниши между собой в Telegram, и то же самое для MAX. "
        "Поэтому сравнивать между платформами корректно только как «сильна/слаба "
        "относительно своей платформы», а не как абсолютные величины.",
        "",
        "## Ниши, сильные сразу на обеих платформах",
        "",
        "| # | Ниша | Слабейшая | Telegram | MAX | Разрыв | Каналов TG | Каналов MAX |",
        "|---:|---|---:|---:|---:|---:|---:|---:|",
    ]

    for position, niche in enumerate(both, start=1):
        flag = " ⚠" if niche.is_asymmetric else ""
        lines.append(
            f"| {position} | {niche.name}{flag} | **{_fmt(niche.weakest_score)}** "
            f"| {_fmt(niche.score_telegram)} | {_fmt(niche.score_max)} "
            f"| {_fmt(niche.gap)} | {niche.channels_telegram} | {niche.channels_max} |"
        )

    if not both:
        lines.append("| — | нет ниш, ранжированных на обеих платформах | — | — | — | — | — | — |")

    asymmetric = [niche for niche in both if niche.is_asymmetric]
    if asymmetric:
        lines += [
            "",
            f"⚠ **Асимметричные ниши** (разрыв ≥ {ASYMMETRY_THRESHOLD:.0f} пунктов): "
            + ", ".join(
                f"{niche.name} (TG {_fmt(niche.score_telegram)} / MAX {_fmt(niche.score_max)})"
                for niche in asymmetric
            )
            + ". Единый бренд в такой нише будет заметно слабее на одной из платформ.",
        ]

    if single:
        lines += [
            "",
            "## Ранжированы только на одной платформе",
            "",
            "Обычно это значит, что на второй платформе в нише слишком мало каналов "
            "для устойчивой статистики — не то же самое, что «ниша там плохая».",
            "",
            "| Ниша | Telegram | MAX | Каналов TG | Каналов MAX |",
            "|---|---:|---:|---:|---:|",
        ]
        for niche in single:
            lines.append(
                f"| {niche.name} | {_fmt(niche.score_telegram)} | {_fmt(niche.score_max)} "
                f"| {niche.channels_telegram} | {niche.channels_max} |"
            )

    lines.append("")
    return "\n".join(lines)


def write_cross_platform_report(
    directory: Path,
    telegram_run: NicheScoreRun,
    max_run: NicheScoreRun,
    niches: list[CrossPlatformNiche],
) -> Path:
    """Write the dual-platform comparison report."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / CROSS_PLATFORM_FILE
    path.write_text(render_cross_platform(telegram_run, max_run, niches), encoding="utf-8")
    return path
