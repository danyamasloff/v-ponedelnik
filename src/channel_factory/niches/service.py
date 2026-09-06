"""Orchestration of niche analysis: metrics -> score -> stored run."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from channel_factory.core.enums import Platform
from channel_factory.core.logging import get_logger
from channel_factory.db.models import NicheScore, NicheScoreRun
from channel_factory.db.repositories.niches import NicheScoreRepository
from channel_factory.db.session import Database
from channel_factory.market.statistics import (
    DatasetSummary,
    MarketStatisticsRepository,
    NicheMetrics,
)
from channel_factory.niches.config import NicheScoreConfig
from channel_factory.niches.scoring import NicheScoreResult, score_niches

logger = get_logger(__name__)


def _number(value: Decimal | int | float | None) -> float | None:
    """JSON-friendly number. Stored metrics are for reporting; the authoritative
    values stay in ``market_snapshots``."""
    if value is None:
        return None
    return round(float(value), 6)


def metrics_to_dict(metrics: NicheMetrics) -> dict[str, Any]:
    return {
        "channels_count": metrics.channels_count,
        "subscribers_known": metrics.subscribers_known,
        "median_subscribers": _number(metrics.median_subscribers),
        "p25_subscribers": _number(metrics.p25_subscribers),
        "p75_subscribers": _number(metrics.p75_subscribers),
        "iqr_subscribers": _number(metrics.iqr_subscribers),
        "total_subscribers": _number(metrics.total_subscribers),
        "median_err": _number(metrics.median_err),
        "median_views": _number(metrics.median_views),
        "median_cpv": _number(metrics.median_cpv),
        "median_campaign_price": _number(metrics.median_campaign_price),
        "median_views_to_subs": _number(metrics.median_views_to_subs),
        "top_channel_share": _number(metrics.top_channel_share),
        "median_growth_rate": _number(metrics.median_growth_rate),
        "channels_with_history": metrics.channels_with_history,
    }


def dataset_to_dict(dataset: DatasetSummary) -> dict[str, Any]:
    return {
        "channels": dataset.channels,
        "channels_without_niche": dataset.channels_without_niche,
        "snapshots": dataset.snapshots,
        "snapshot_dates": dataset.snapshot_dates,
        "first_date": dataset.first_date.isoformat() if dataset.first_date else None,
        "last_date": dataset.last_date.isoformat() if dataset.last_date else None,
        "niches": dataset.niches,
        "imports": dataset.imports,
        "platforms": dataset.platforms,
        "sources": list(dataset.sources),
    }


@dataclass
class AnalysisOutcome:
    """Result of one analysis, whether or not it was persisted."""

    score_version: str
    dataset: DatasetSummary
    results: list[NicheScoreResult]
    as_of: date | None = None
    platform: Platform | None = None
    run_id: uuid.UUID | None = None
    persisted: bool = False
    # Below this many scored niches, percentile ranking is coarse and the
    # ranking is reported as low-confidence rather than presented as settled.
    low_confidence_threshold: int = 5

    @property
    def has_data(self) -> bool:
        return not self.dataset.is_empty and bool(self.results)

    @property
    def scored(self) -> list[NicheScoreResult]:
        return sorted(
            (r for r in self.results if r.score is not None),
            key=lambda r: r.rank or 0,
        )

    @property
    def skipped(self) -> list[NicheScoreResult]:
        return [r for r in self.results if r.score is None]

    @property
    def low_confidence(self) -> bool:
        """True when too few niches were scored for percentile ranking to mean much."""
        return 0 < len(self.scored) < self.low_confidence_threshold


class NicheAnalysisService:
    """Computes and stores niche rankings."""

    def __init__(self, database: Database, config: NicheScoreConfig) -> None:
        self._database = database
        self._config = config

    async def analyze(
        self,
        *,
        as_of: date | None = None,
        persist: bool = True,
        platform: Platform | None = None,
    ) -> AnalysisOutcome:
        """Aggregate the market, score every niche and (optionally) store the run.

        ``platform`` scopes the whole analysis to one messenger. Percentile ranks
        are then computed inside that platform, which is what makes a Telegram
        ranking and a MAX ranking independently meaningful.
        """
        async with self._database.session() as session:
            stats = MarketStatisticsRepository(session)
            dataset = await stats.dataset_summary(platform=platform)
            if dataset.is_empty:
                logger.info("niche analysis: no market data available")
                return AnalysisOutcome(
                    score_version=self._config.score_version,
                    dataset=dataset,
                    results=[],
                    as_of=as_of,
                    platform=platform,
                )

            metrics = await stats.niche_metrics(
                as_of=as_of,
                top_n=self._config.top_channels_for_concentration,
                platform=platform,
            )

        results = score_niches(metrics, self._config)
        outcome = AnalysisOutcome(
            score_version=self._config.score_version,
            dataset=dataset,
            results=results,
            as_of=as_of,
            platform=platform,
            low_confidence_threshold=self._config.low_confidence_niche_count,
        )

        if persist and results:
            outcome.run_id = await self._persist(outcome)
            outcome.persisted = True

        logger.info(
            "niche analysis finished",
            extra={
                "score_version": outcome.score_version,
                "platform": outcome.platform.value if outcome.platform else "ALL",
                "niches_scored": len(outcome.scored),
                "niches_skipped": len(outcome.skipped),
                "persisted": outcome.persisted,
                "run_id": str(outcome.run_id) if outcome.run_id else "-",
            },
        )
        return outcome

    async def _persist(self, outcome: AnalysisOutcome) -> uuid.UUID:
        async with self._database.session() as session:
            run = NicheScoreRun(
                score_version=outcome.score_version,
                platform=outcome.platform,
                as_of_date=outcome.as_of,
                niches_scored=len(outcome.scored),
                niches_skipped=len(outcome.skipped),
                low_confidence=outcome.low_confidence,
                dataset=dataset_to_dict(outcome.dataset),
                config={
                    "weights": self._config.weights,
                    "min_channels_for_scoring": self._config.min_channels_for_scoring,
                    "top_channels_for_concentration": (
                        self._config.top_channels_for_concentration
                    ),
                    "manual_defaults": self._config.manual_defaults,
                    "manual_overrides": self._config.manual_overrides,
                },
            )
            NicheScoreRepository(session).add_run(run)
            await session.flush()

            session.add_all(
                [
                    NicheScore(
                        run_id=run.id,
                        niche_id=uuid.UUID(result.metrics.niche_id),
                        score_version=outcome.score_version,
                        score=None if result.score is None else Decimal(str(result.score)),
                        rank=result.rank,
                        channels_count=result.metrics.channels_count,
                        insufficient_data=result.insufficient_data,
                        components=result.components_dict(),
                        metrics=metrics_to_dict(result.metrics),
                    )
                    for result in outcome.results
                ]
            )
            await session.commit()
            return run.id
