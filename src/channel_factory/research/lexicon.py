"""Deterministic relevance and practical value.

These two components would otherwise need an LLM, and without one the score
answers only "is it fresh and from an official source" — true of nearly
everything in a curated whitelist. A lexicon is explicitly a heuristic, not a
judgement, but it costs nothing, runs offline, is testable, and every number it
produces can name the words that produced it.

An LLM verdict, when available, takes precedence: this is the floor, not the
ceiling.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from channel_factory.core.enums import EventType
from channel_factory.research.canonical import normalize_text


class LexiconError(Exception):
    """Raised when the lexicon configuration is invalid."""


@dataclass(frozen=True)
class LexiconVerdict:
    """A component value plus the terms that produced it."""

    value: float
    matched: tuple[str, ...] = ()
    penalised: tuple[str, ...] = ()

    def explain(self) -> str:
        parts = []
        if self.matched:
            parts.append("+" + ", ".join(self.matched[:6]))
        if self.penalised:
            parts.append("-" + ", ".join(self.penalised[:6]))
        return "; ".join(parts) or "no lexicon terms matched"


@dataclass(frozen=True)
class TopicLexicon:
    """Weighted terms for the deterministic components."""

    relevance_base: float
    relevance_positive: dict[str, float]
    relevance_negative: dict[str, float]
    relevance_max_positive: float
    relevance_max_negative: float
    practical_base: float
    practical_patterns: dict[str, float]
    practical_max_bonus: float
    event_type_bonus: dict[str, float] = field(default_factory=dict)
    version_bonus: float = 0.0

    def relevance(self, text: str) -> LexiconVerdict:
        """How much this looks like applying AI and digital tools."""
        normalized = normalize_text(text)
        matched, positive = _accumulate(normalized, self.relevance_positive)
        penalised, negative = _accumulate(normalized, self.relevance_negative)
        value = (
            self.relevance_base
            + min(positive, self.relevance_max_positive)
            - min(negative, self.relevance_max_negative)
        )
        return LexiconVerdict(
            value=_clamp(value), matched=matched, penalised=penalised
        )

    def practical_value(
        self, title: str, summary: str | None, event_type: EventType, *, has_version: bool
    ) -> LexiconVerdict:
        """How likely a reader can act on this.

        The title carries most of the signal: a headline that promises an action
        is the strongest thing we can read without a model. The body is scanned
        too, at half weight, so a practical article with a bland headline is not
        lost entirely.
        """
        title_matched, title_score = _accumulate(normalize_text(title), self.practical_patterns)
        body_matched, body_score = _accumulate(
            normalize_text(summary or ""), self.practical_patterns
        )
        bonus = title_score + body_score * 0.5
        bonus += self.event_type_bonus.get(event_type.value, 0.0)
        if has_version:
            bonus += self.version_bonus

        matched = tuple(dict.fromkeys(title_matched + body_matched))
        return LexiconVerdict(
            value=_clamp(self.practical_base + min(bonus, self.practical_max_bonus)),
            matched=matched,
        )


def _accumulate(text: str, terms: dict[str, float]) -> tuple[tuple[str, ...], float]:
    """Sum the weights of terms present, counting each term once."""
    if not text:
        return (), 0.0
    matched: list[str] = []
    total = 0.0
    for term, weight in terms.items():
        if term in text:
            matched.append(term)
            total += weight
    return tuple(matched), total


def _clamp(value: float) -> float:
    return round(max(0.0, min(100.0, value)), 2)


def _weights(raw: Any, where: str) -> dict[str, float]:
    if not isinstance(raw, dict) or not raw:
        raise LexiconError(f"{where}: expected a non-empty mapping of term -> weight")
    weights: dict[str, float] = {}
    for term, weight in raw.items():
        text = normalize_text(str(term))
        if not text:
            raise LexiconError(f"{where}: empty term")
        try:
            weights[text] = float(weight)
        except (TypeError, ValueError) as exc:
            raise LexiconError(f"{where}: weight for {term!r} is not a number") from exc
        if weights[text] < 0:
            raise LexiconError(f"{where}: weight for {term!r} must not be negative")
    return weights


def load_topic_lexicon(path: Path) -> TopicLexicon:
    """Load and validate ``config/topic_lexicon.yaml``."""
    if not path.exists():
        raise LexiconError(f"topic lexicon not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    relevance = raw.get("relevance") or {}
    practical = raw.get("practical_value") or {}
    if not relevance or not practical:
        raise LexiconError(f"both 'relevance' and 'practical_value' are required in {path}")

    bonuses = practical.get("event_type_bonus") or {}
    unknown = set(bonuses) - {member.value for member in EventType}
    if unknown:
        raise LexiconError(f"unknown event types in event_type_bonus: {sorted(unknown)}")

    return TopicLexicon(
        relevance_base=float(relevance.get("base", 40)),
        relevance_positive=_weights(relevance.get("positive"), "relevance.positive"),
        relevance_negative=_weights(relevance.get("negative"), "relevance.negative"),
        relevance_max_positive=float(relevance.get("max_positive", 60)),
        relevance_max_negative=float(relevance.get("max_negative", 55)),
        practical_base=float(practical.get("base", 35)),
        practical_patterns=_weights(practical.get("patterns"), "practical_value.patterns"),
        practical_max_bonus=float(practical.get("max_bonus", 65)),
        event_type_bonus={str(key): float(value) for key, value in bonuses.items()},
        version_bonus=float(practical.get("version_bonus", 0)),
    )
