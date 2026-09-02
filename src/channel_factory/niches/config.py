"""Loading and validating the Niche Score configuration."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

WEIGHT_SUM_TOLERANCE = 1e-6
RATING_MIN, RATING_MAX = 0, 100

# Subjective inputs the configuration must provide a default for.
MANUAL_RATING_KEYS = (
    "content_scalability",
    "production_cost",
    "originality_potential",
    "brand_safety",
    "monetization_risk",
)


class NicheScoreConfigError(Exception):
    """Raised when the scoring configuration is invalid."""


@dataclass(frozen=True)
class NicheScoreConfig:
    """Weights, thresholds and subjective ratings for one score version."""

    score_version: str
    weights: dict[str, float]
    manual_defaults: dict[str, int]
    manual_overrides: dict[str, dict[str, int]]
    min_channels_for_scoring: int
    top_channels_for_concentration: int
    low_confidence_niche_count: int

    def manual_rating(self, slug: str, key: str) -> tuple[int, bool]:
        """Return ``(rating, is_explicit)`` for a niche.

        ``is_explicit`` is False when the neutral default is used, which the
        reports surface so a score is never mistaken for a judgement the user
        actually made.
        """
        override = self.manual_overrides.get(slug, {})
        if key in override:
            return override[key], True
        return self.manual_defaults[key], False

    def has_overrides(self, slug: str) -> bool:
        return bool(self.manual_overrides.get(slug))


def _validate_rating(where: str, key: str, value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise NicheScoreConfigError(f"{where}: rating '{key}' must be an integer, got {value!r}")
    if not RATING_MIN <= value <= RATING_MAX:
        raise NicheScoreConfigError(
            f"{where}: rating '{key}' must be between {RATING_MIN} and {RATING_MAX}, got {value}"
        )
    return value


def load_niche_score_config(path: Path) -> NicheScoreConfig:
    """Load and validate ``config/niche_score.yaml``."""
    if not path.exists():
        raise NicheScoreConfigError(f"niche score config not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}

    weights_raw = raw.get("weights")
    if not isinstance(weights_raw, dict) or not weights_raw:
        raise NicheScoreConfigError(f"'weights' section is missing or empty in {path}")
    weights = {str(key): float(value) for key, value in weights_raw.items()}
    if any(weight < 0 for weight in weights.values()):
        raise NicheScoreConfigError("weights must not be negative")
    total = sum(weights.values())
    if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
        raise NicheScoreConfigError(f"weights must sum to 1.0, got {total:.6f}")

    defaults_raw = raw.get("manual_defaults") or {}
    missing = [key for key in MANUAL_RATING_KEYS if key not in defaults_raw]
    if missing:
        raise NicheScoreConfigError(f"manual_defaults is missing: {missing}")
    manual_defaults = {
        key: _validate_rating("manual_defaults", key, defaults_raw[key])
        for key in MANUAL_RATING_KEYS
    }

    overrides_raw = raw.get("manual_overrides") or {}
    if not isinstance(overrides_raw, dict):
        raise NicheScoreConfigError("'manual_overrides' must be a mapping of slug -> ratings")
    manual_overrides: dict[str, dict[str, int]] = {}
    for slug, ratings in overrides_raw.items():
        if not isinstance(ratings, dict):
            raise NicheScoreConfigError(f"manual_overrides['{slug}'] must be a mapping")
        unknown = set(ratings) - set(MANUAL_RATING_KEYS)
        if unknown:
            raise NicheScoreConfigError(
                f"manual_overrides['{slug}'] has unknown ratings: {sorted(unknown)}"
            )
        manual_overrides[str(slug)] = {
            key: _validate_rating(f"manual_overrides['{slug}']", key, value)
            for key, value in ratings.items()
        }

    return NicheScoreConfig(
        score_version=str(raw.get("score_version", "niche_score_v1")),
        weights=weights,
        manual_defaults=manual_defaults,
        manual_overrides=manual_overrides,
        min_channels_for_scoring=int(raw.get("min_channels_for_scoring", 3)),
        top_channels_for_concentration=int(raw.get("top_channels_for_concentration", 3)),
        low_confidence_niche_count=int(raw.get("low_confidence_niche_count", 5)),
    )
