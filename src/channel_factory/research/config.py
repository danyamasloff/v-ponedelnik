"""Loading and validating the research engine's configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from channel_factory.core.enums import EventType, ResearchProviderType, TrustLevel
from channel_factory.research.providers.base import SourceConfig

WEIGHT_SUM_TOLERANCE = 1e-6

# Every component the scorer knows about. A weight for anything else, or a
# missing weight for one of these, is a configuration error rather than a
# silently ignored line.
TOPIC_COMPONENTS = (
    "relevance",
    "practical_value",
    "novelty",
    "authority",
    "timeliness",
    "audience_fit",
    "content_potential",
)

# Components that need an LLM. Without a key or within a cost limit they become
# unavailable and their weight is redistributed, exactly like a missing metric
# in the niche score.
LLM_COMPONENTS = frozenset({"relevance", "practical_value", "audience_fit", "content_potential"})


#: Can the channel's audience open this source without a VPN?
AUDIENCE_ACCESS_VALUES = frozenset({"RU", "VPN"})


class ResearchConfigError(Exception):
    """Raised when research configuration is invalid."""


@dataclass(frozen=True)
class TopicScoreConfig:
    """Weights, thresholds and decay settings for Topic Score."""

    score_version: str
    weights: dict[str, float]
    selected_threshold: float
    backlog_threshold: float
    timeliness_half_life_hours: dict[str, float]
    similar_simhash_distance: int
    age_penalty_per_day: float
    expire_after_days: int
    low_confidence_cluster_count: int

    def half_life_for(self, event_type: EventType) -> float:
        return self.timeliness_half_life_hours.get(
            event_type.value, self.timeliness_half_life_hours.get("OTHER", 120.0)
        )


def load_topic_score_config(path: Path) -> TopicScoreConfig:
    """Load and validate ``config/topic_score.yaml``."""
    if not path.exists():
        raise ResearchConfigError(f"topic score config not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    weights_raw = raw.get("weights")
    if not isinstance(weights_raw, dict) or not weights_raw:
        raise ResearchConfigError(f"'weights' is missing or empty in {path}")
    weights = {str(key): float(value) for key, value in weights_raw.items()}

    unknown = set(weights) - set(TOPIC_COMPONENTS)
    if unknown:
        raise ResearchConfigError(f"unknown score components: {sorted(unknown)}")
    missing = set(TOPIC_COMPONENTS) - set(weights)
    if missing:
        raise ResearchConfigError(f"missing weights for components: {sorted(missing)}")
    if any(weight < 0 for weight in weights.values()):
        raise ResearchConfigError("weights must not be negative")
    total = sum(weights.values())
    if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
        raise ResearchConfigError(f"weights must sum to 1.0, got {total:.6f}")

    thresholds = raw.get("thresholds") or {}
    selected = float(thresholds.get("selected", 70))
    backlog = float(thresholds.get("backlog", 45))
    if backlog >= selected:
        raise ResearchConfigError(
            f"backlog threshold ({backlog}) must be below selected ({selected})"
        )

    novelty = raw.get("novelty") or {}
    return TopicScoreConfig(
        score_version=str(raw.get("score_version", "topic_score_v1")),
        weights=weights,
        selected_threshold=selected,
        backlog_threshold=backlog,
        timeliness_half_life_hours={
            str(key): float(value)
            for key, value in (raw.get("timeliness_half_life_hours") or {}).items()
        },
        similar_simhash_distance=int(novelty.get("similar_simhash_distance", 12)),
        age_penalty_per_day=float(novelty.get("age_penalty_per_day", 8)),
        expire_after_days=int(raw.get("expire_after_days", 30)),
        low_confidence_cluster_count=int(raw.get("low_confidence_cluster_count", 5)),
    )


def load_source_configs(path: Path) -> list[SourceConfig]:
    """Load and validate ``config/research_sources.yaml``."""
    if not path.exists():
        raise ResearchConfigError(f"research sources config not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries = raw.get("sources")
    if not isinstance(entries, list) or not entries:
        raise ResearchConfigError(f"'sources' is missing or empty in {path}")

    defaults = raw.get("defaults") or {}
    default_interval = int(defaults.get("poll_interval_minutes", 60))
    default_enabled = bool(defaults.get("enabled", True))

    sources: list[SourceConfig] = []
    seen: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise ResearchConfigError(f"source entry must be a mapping, got {entry!r}")
        key = str(entry.get("key") or "").strip()
        if not key:
            raise ResearchConfigError(f"source without a key: {entry!r}")
        if key in seen:
            raise ResearchConfigError(f"duplicate source key: {key!r}")
        seen.add(key)

        try:
            provider = ResearchProviderType(str(entry["provider"]))
        except (KeyError, ValueError) as exc:
            raise ResearchConfigError(
                f"source {key!r}: unknown or missing provider {entry.get('provider')!r}"
            ) from exc
        try:
            trust = TrustLevel(str(entry["trust"]))
        except (KeyError, ValueError) as exc:
            raise ResearchConfigError(
                f"source {key!r}: unknown or missing trust {entry.get('trust')!r}"
            ) from exc

        url = entry.get("url")
        repo = entry.get("repo")
        if provider in {
            ResearchProviderType.RSS,
            ResearchProviderType.OFFICIAL_BLOG,
        } and not url:
            raise ResearchConfigError(f"source {key!r}: provider {provider} requires a url")
        if provider is ResearchProviderType.GITHUB_RELEASES and not repo:
            raise ResearchConfigError(f"source {key!r}: GITHUB_RELEASES requires a repo")

        # Default VPN, not RU: assuming a foreign source is reachable is the
        # mistake that puts dead links in front of readers.
        access = str(entry.get("audience_access", "VPN")).upper()
        if access not in AUDIENCE_ACCESS_VALUES:
            raise ResearchConfigError(
                f"source {key!r}: audience_access must be one of "
                f"{sorted(AUDIENCE_ACCESS_VALUES)}, got {access!r}"
            )

        sources.append(
            SourceConfig(
                key=key,
                name=str(entry.get("name") or key),
                provider=provider,
                trust=trust,
                url=str(url) if url else None,
                repo=str(repo) if repo else None,
                enabled=bool(entry.get("enabled", default_enabled)),
                poll_interval_minutes=int(
                    entry.get("poll_interval_minutes", default_interval)
                ),
                reason=entry.get("reason"),
                audience_access=access,
                config=entry.get("config") or {},
            )
        )
    return sources
