"""Niche Score v1.

The model in one line::

    score = sum(weight_i * component_i) / sum(weight_i)   over AVAILABLE components

Every component is scaled to 0-100 with "higher is better" orientation, so the
formula is a plain weighted average with no sign handling. Two deliberate
properties:

* **Percentile rank, not min-max.** Data components are ranked against the other
  niches in the same dataset. Min-max scaling would let one outlier niche
  compress everyone else into a narrow band; percentile rank cannot.
* **Renormalization instead of imputation.** A component that cannot be computed
  (no CPV in the source, no second snapshot date for growth) is dropped for that
  niche and the remaining weights are rescaled. Missing data is never filled
  with an invented value.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from channel_factory.market.statistics import NicheMetrics
from channel_factory.niches.config import NicheScoreConfig

DATA_SOURCE = "data"
MANUAL_SOURCE = "manual"


@dataclass(frozen=True)
class ComponentSpec:
    """Definition of one score component."""

    key: str
    label: str
    source: str
    metric: str | None = None
    rating: str | None = None
    invert: bool = False
    unit: str | None = None


# Data-derived components carry 0.75 of the weight in the shipped configuration.
DATA_COMPONENTS: tuple[ComponentSpec, ...] = (
    ComponentSpec("audience_value", "Audience value (CPV)", DATA_SOURCE, metric="median_cpv"),
    ComponentSpec(
        "ad_budget_level", "Ad budget level", DATA_SOURCE, metric="median_campaign_price"
    ),
    ComponentSpec("engagement", "Engagement (ERR)", DATA_SOURCE, metric="median_err"),
    ComponentSpec(
        "reach_efficiency", "Reach efficiency", DATA_SOURCE, metric="median_views_to_subs"
    ),
    ComponentSpec("market_size", "Market size", DATA_SOURCE, metric="channels_count"),
    ComponentSpec(
        "low_competition",
        "Low competition",
        DATA_SOURCE,
        metric="top_channel_share",
        invert=True,
    ),
    ComponentSpec(
        "growth_potential", "Growth potential", DATA_SOURCE, metric="median_growth_rate"
    ),
)

MANUAL_COMPONENTS: tuple[ComponentSpec, ...] = (
    ComponentSpec(
        "content_scalability", "Content scalability", MANUAL_SOURCE, rating="content_scalability"
    ),
    ComponentSpec(
        "low_production_cost",
        "Low production cost",
        MANUAL_SOURCE,
        rating="production_cost",
        invert=True,
    ),
    ComponentSpec(
        "originality_potential",
        "Originality potential",
        MANUAL_SOURCE,
        rating="originality_potential",
    ),
    ComponentSpec("brand_safety", "Brand safety", MANUAL_SOURCE, rating="brand_safety"),
    ComponentSpec(
        "low_monetization_risk",
        "Low monetization risk",
        MANUAL_SOURCE,
        rating="monetization_risk",
        invert=True,
    ),
)

ALL_COMPONENTS: tuple[ComponentSpec, ...] = DATA_COMPONENTS + MANUAL_COMPONENTS


@dataclass
class ComponentValue:
    """One component's contribution to one niche's score."""

    key: str
    label: str
    source: str
    weight: float
    value: float | None
    available: bool
    raw_value: float | None = None
    is_default_rating: bool = False
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "source": self.source,
            "weight": round(self.weight, 6),
            "value": None if self.value is None else round(self.value, 2),
            "raw_value": self.raw_value,
            "available": self.available,
            "is_default_rating": self.is_default_rating,
            "note": self.note,
        }


@dataclass
class NicheScoreResult:
    """Scoring outcome for one niche."""

    metrics: NicheMetrics
    score: float | None
    components: list[ComponentValue] = field(default_factory=list)
    insufficient_data: bool = False
    rank: int | None = None

    @property
    def slug(self) -> str:
        return self.metrics.slug

    @property
    def name(self) -> str:
        return self.metrics.name

    @property
    def uses_only_default_ratings(self) -> bool:
        return all(
            component.is_default_rating
            for component in self.components
            if component.source == MANUAL_SOURCE
        )

    @property
    def unavailable_components(self) -> tuple[str, ...]:
        return tuple(c.key for c in self.components if not c.available)

    def components_dict(self) -> dict[str, Any]:
        return {component.key: component.to_dict() for component in self.components}


def percentile_ranks(values: Sequence[float | None]) -> list[float | None]:
    """Percentile rank of each value within the population of known values.

    Uses the average-rank definition ``(below + 0.5 * equal) / n * 100``, so
    tied values share a rank and a single-element population lands on 50 rather
    than a misleading 0 or 100. ``None`` stays ``None`` and is excluded from the
    population.
    """
    known = [value for value in values if value is not None]
    count = len(known)
    if count == 0:
        return [None] * len(values)

    ranks: list[float | None] = []
    for value in values:
        if value is None:
            ranks.append(None)
            continue
        below = sum(1 for other in known if other < value)
        equal = sum(1 for other in known if other == value)
        ranks.append((below + 0.5 * equal) / count * 100)
    return ranks


def _metric_value(metrics: NicheMetrics, name: str) -> float | None:
    value = getattr(metrics, name, None)
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return float(value)


def score_niches(
    metrics: Sequence[NicheMetrics], config: NicheScoreConfig
) -> list[NicheScoreResult]:
    """Score every niche and rank the ones with enough data.

    Niches below ``min_channels_for_scoring`` are returned unscored and flagged:
    a median over one or two channels describes those channels, not the niche.
    """
    eligible = [m for m in metrics if m.channels_count >= config.min_channels_for_scoring]

    # Percentile ranks are computed over eligible niches only, so a thin niche
    # cannot distort the scale everyone else is measured against.
    ranked_by_component: dict[str, list[float | None]] = {}
    for spec in DATA_COMPONENTS:
        assert spec.metric is not None
        raw = [_metric_value(m, spec.metric) for m in eligible]
        ranks = percentile_ranks(raw)
        if spec.invert:
            ranks = [None if rank is None else 100.0 - rank for rank in ranks]
        ranked_by_component[spec.key] = ranks

    eligible_index = {m.niche_id: position for position, m in enumerate(eligible)}
    results: list[NicheScoreResult] = []

    for niche in metrics:
        position = eligible_index.get(niche.niche_id)
        if position is None:
            results.append(
                NicheScoreResult(metrics=niche, score=None, insufficient_data=True)
            )
            continue

        components: list[ComponentValue] = []
        for spec in DATA_COMPONENTS:
            weight = config.weights.get(spec.key, 0.0)
            value = ranked_by_component[spec.key][position]
            assert spec.metric is not None
            raw_value = _metric_value(niche, spec.metric)
            components.append(
                ComponentValue(
                    key=spec.key,
                    label=spec.label,
                    source=spec.source,
                    weight=weight,
                    value=value,
                    available=value is not None,
                    raw_value=raw_value,
                    note=None if value is not None else "no data in the current dataset",
                )
            )

        for spec in MANUAL_COMPONENTS:
            assert spec.rating is not None
            rating, is_explicit = config.manual_rating(niche.slug, spec.rating)
            value = float(100 - rating) if spec.invert else float(rating)
            components.append(
                ComponentValue(
                    key=spec.key,
                    label=spec.label,
                    source=spec.source,
                    weight=config.weights.get(spec.key, 0.0),
                    value=value,
                    available=True,
                    raw_value=float(rating),
                    is_default_rating=not is_explicit,
                    note=None if is_explicit else "neutral default, not a judgement yet",
                )
            )

        available = [c for c in components if c.available and c.weight > 0]
        weight_sum = sum(c.weight for c in available)
        score = (
            None
            if weight_sum == 0
            else sum(c.weight * (c.value or 0.0) for c in available) / weight_sum
        )
        results.append(
            NicheScoreResult(
                metrics=niche,
                score=None if score is None else round(score, 2),
                components=components,
                insufficient_data=False,
            )
        )

    scored = sorted(
        (r for r in results if r.score is not None),
        key=lambda r: r.score or 0.0,
        reverse=True,
    )
    for rank, result in enumerate(scored, start=1):
        result.rank = rank
    return results
