"""Topic Score v1.

Same model as Niche Score, for the same reasons::

    score = sum(weight_i * component_i) / sum(weight_i)   over AVAILABLE components

Every component is 0-100 with "higher is better", and a component that cannot
be computed is dropped with the remaining weights renormalized rather than
filled with a made-up number.

That property does real work here: four of the seven components need an LLM, so
without an API key — or once a cost limit is reached — the engine still scores
every cluster from novelty, authority and timeliness alone, and says so.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from channel_factory.core.enums import ClusterStatus, EventType, TrustLevel, trust_authority_score
from channel_factory.research.config import LLM_COMPONENTS, TopicScoreConfig
from channel_factory.research.lexicon import LexiconVerdict

DATA_SOURCE = "data"
LLM_SOURCE = "llm"
LEXICON_SOURCE = "lexicon"

COMPONENT_LABELS = {
    "relevance": "Relevance to the niche",
    "practical_value": "Practical value",
    "novelty": "Novelty",
    "authority": "Source authority",
    "timeliness": "Timeliness",
    "audience_fit": "Audience fit",
    "content_potential": "Content potential",
}


@dataclass
class ScoreComponent:
    """One component's contribution."""

    key: str
    label: str
    source: str
    weight: float
    value: float | None
    available: bool
    note: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "source": self.source,
            "weight": round(self.weight, 6),
            "value": None if self.value is None else round(self.value, 2),
            "available": self.available,
            "note": self.note,
        }


@dataclass
class ClusterScoreInput:
    """Everything the scorer needs about one cluster."""

    cluster_key: str
    event_type: EventType
    max_trust: TrustLevel
    event_at: datetime | None
    first_seen_at: datetime
    item_count: int
    distinct_sources: int = 1
    similar_to_published: bool = False
    llm_judgement: dict[str, float] | None = None
    # Free, deterministic fallbacks for two of the four judgement components.
    # An LLM verdict wins when there is one; this is the floor, not the ceiling.
    lexicon: dict[str, LexiconVerdict] | None = None


@dataclass
class TopicScoreResult:
    """Outcome of scoring one cluster."""

    score: float | None
    decision: ClusterStatus
    components: list[ScoreComponent] = field(default_factory=list)
    inputs: dict[str, Any] = field(default_factory=dict)

    @property
    def unavailable(self) -> tuple[str, ...]:
        return tuple(component.key for component in self.components if not component.available)

    @property
    def used_llm(self) -> bool:
        return any(
            component.available and component.source == LLM_SOURCE
            for component in self.components
        )

    @property
    def used_lexicon(self) -> bool:
        return any(
            component.available and component.source == LEXICON_SOURCE
            for component in self.components
        )

    def components_dict(self) -> dict[str, Any]:
        return {component.key: component.to_dict() for component in self.components}


def timeliness_value(
    event_at: datetime, *, now: datetime, half_life_hours: float
) -> float:
    """Exponential decay: 100 at publication, 50 after one half-life."""
    age_hours = max((now - event_at).total_seconds() / 3600.0, 0.0)
    if half_life_hours <= 0:
        return 0.0
    return round(100.0 * (0.5 ** (age_hours / half_life_hours)), 2)


def novelty_value(
    *,
    first_seen_at: datetime,
    now: datetime,
    similar_to_published: bool,
    age_penalty_per_day: float,
) -> float:
    """How new this still is to us.

    Something we have already published about is not novel, whatever its date.
    """
    if similar_to_published:
        return 0.0
    age_days = max((now - first_seen_at).total_seconds() / 86400.0, 0.0)
    return round(max(0.0, 100.0 - age_days * age_penalty_per_day), 2)


def score_cluster(
    data: ClusterScoreInput,
    config: TopicScoreConfig,
    *,
    now: datetime | None = None,
) -> TopicScoreResult:
    """Score one cluster and decide what happens to it."""
    moment = now or datetime.now(UTC)
    event_at = data.event_at or data.first_seen_at
    components: list[ScoreComponent] = []

    deterministic: dict[str, float] = {
        "authority": float(trust_authority_score(data.max_trust)),
        "timeliness": timeliness_value(
            event_at, now=moment, half_life_hours=config.half_life_for(data.event_type)
        ),
        "novelty": novelty_value(
            first_seen_at=data.first_seen_at,
            now=moment,
            similar_to_published=data.similar_to_published,
            age_penalty_per_day=config.age_penalty_per_day,
        ),
    }

    judgement = data.llm_judgement or {}
    lexicon = data.lexicon or {}

    for key in config.weights:
        weight = config.weights[key]
        label = COMPONENT_LABELS.get(key, key)
        if key in LLM_COMPONENTS:
            raw = judgement.get(key)
            if raw is not None:
                components.append(
                    ScoreComponent(
                        key=key,
                        label=label,
                        source=LLM_SOURCE,
                        weight=weight,
                        value=float(max(0.0, min(100.0, raw))),
                        available=True,
                    )
                )
                continue

            verdict = lexicon.get(key)
            if verdict is not None:
                components.append(
                    ScoreComponent(
                        key=key,
                        label=label,
                        source=LEXICON_SOURCE,
                        weight=weight,
                        value=verdict.value,
                        available=True,
                        note=verdict.explain(),
                    )
                )
                continue

            components.append(
                ScoreComponent(
                    key=key,
                    label=label,
                    source=LLM_SOURCE,
                    weight=weight,
                    value=None,
                    available=False,
                    note="no LLM judgement and no lexicon fallback for this component",
                )
            )
        else:
            components.append(
                ScoreComponent(
                    key=key,
                    label=label,
                    source=DATA_SOURCE,
                    weight=weight,
                    value=deterministic[key],
                    available=True,
                )
            )

    usable = [c for c in components if c.available and c.weight > 0]
    weight_sum = sum(c.weight for c in usable)
    score = (
        None
        if weight_sum == 0
        else round(sum(c.weight * (c.value or 0.0) for c in usable) / weight_sum, 2)
    )

    if score is None:
        decision = ClusterStatus.OPEN
    elif score >= config.selected_threshold:
        decision = ClusterStatus.SELECTED
    elif score >= config.backlog_threshold:
        decision = ClusterStatus.BACKLOG
    else:
        # Never REJECTED: a weak score is a parking decision, and the cluster
        # is re-scored when new supporting items arrive.
        decision = ClusterStatus.FILTERED

    return TopicScoreResult(
        score=score,
        decision=decision,
        components=components,
        inputs={
            "cluster_key": data.cluster_key,
            "event_type": data.event_type.value,
            "max_trust": data.max_trust.value,
            "item_count": data.item_count,
            "distinct_sources": data.distinct_sources,
            "event_at": event_at.isoformat(),
            "scored_at": moment.isoformat(),
            "similar_to_published": data.similar_to_published,
        },
    )
