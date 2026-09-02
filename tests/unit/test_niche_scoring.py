"""Unit tests for percentile ranking and the Niche Score v1 formula."""

from __future__ import annotations

from decimal import Decimal

import pytest

from channel_factory.market.statistics import NicheMetrics
from channel_factory.niches.config import NicheScoreConfig
from channel_factory.niches.scoring import percentile_ranks, score_niches


def make_metrics(
    slug: str,
    *,
    channels: int = 5,
    cpv: str | None = "1.00",
    price: str | None = "10000",
    err: str | None = "0.20",
    views_to_subs: str | None = "0.30",
    concentration: str | None = "0.50",
    growth: str | None = None,
) -> NicheMetrics:
    def dec(value: str | None) -> Decimal | None:
        return None if value is None else Decimal(value)

    return NicheMetrics(
        niche_id=f"id-{slug}",
        slug=slug,
        name=slug.upper(),
        channels_count=channels,
        subscribers_known=channels,
        median_subscribers=Decimal("10000"),
        median_cpv=dec(cpv),
        median_campaign_price=dec(price),
        median_err=dec(err),
        median_views_to_subs=dec(views_to_subs),
        top_channel_share=dec(concentration),
        median_growth_rate=dec(growth),
    )


def make_config(**overrides) -> NicheScoreConfig:
    defaults = {
        "score_version": "test_v1",
        "weights": {"engagement": 0.5, "audience_value": 0.5},
        "manual_defaults": {
            "content_scalability": 50,
            "production_cost": 50,
            "originality_potential": 50,
            "brand_safety": 50,
            "monetization_risk": 50,
        },
        "manual_overrides": {},
        "min_channels_for_scoring": 3,
        "top_channels_for_concentration": 3,
        "low_confidence_niche_count": 5,
    }
    return NicheScoreConfig(**{**defaults, **overrides})


class TestPercentileRanks:
    def test_orders_values(self) -> None:
        assert percentile_ranks([1.0, 2.0, 3.0]) == [
            pytest.approx(16.67, abs=0.01),
            50.0,
            pytest.approx(83.33, abs=0.01),
        ]

    def test_single_value_lands_in_the_middle(self) -> None:
        assert percentile_ranks([42.0]) == [50.0]

    def test_ties_share_a_rank(self) -> None:
        assert percentile_ranks([5.0, 5.0]) == [50.0, 50.0]

    def test_none_is_excluded_from_the_population(self) -> None:
        ranks = percentile_ranks([1.0, None, 3.0])
        assert ranks[1] is None
        assert ranks[0] == 25.0
        assert ranks[2] == 75.0

    def test_all_none(self) -> None:
        assert percentile_ranks([None, None]) == [None, None]

    def test_empty(self) -> None:
        assert percentile_ranks([]) == []


class TestScoreNiches:
    def test_higher_metric_scores_higher(self) -> None:
        metrics = [
            make_metrics("low", err="0.05", cpv="0.5"),
            make_metrics("high", err="0.40", cpv="2.0"),
        ]
        results = {r.slug: r for r in score_niches(metrics, make_config())}
        assert results["high"].score > results["low"].score
        assert results["high"].rank == 1
        assert results["low"].rank == 2

    def test_inverted_component_rewards_low_values(self) -> None:
        config = make_config(weights={"low_competition": 1.0})
        metrics = [
            make_metrics("concentrated", concentration="0.90"),
            make_metrics("open", concentration="0.10"),
        ]
        results = {r.slug: r for r in score_niches(metrics, config)}
        assert results["open"].score > results["concentrated"].score

    def test_unavailable_component_is_dropped_and_weights_renormalized(self) -> None:
        """With CPV missing everywhere, the score must come from engagement alone."""
        config = make_config(weights={"engagement": 0.5, "audience_value": 0.5})
        metrics = [
            make_metrics("a", cpv=None, err="0.10"),
            make_metrics("b", cpv=None, err="0.30"),
        ]
        results = {r.slug: r for r in score_niches(metrics, config)}

        assert results["b"].score == 75.0, "engagement percentile alone, not diluted by a zero"
        assert results["a"].score == 25.0
        assert "audience_value" in results["b"].unavailable_components

    def test_growth_is_unavailable_without_history(self) -> None:
        config = make_config(weights={"growth_potential": 0.5, "engagement": 0.5})
        results = score_niches([make_metrics("a"), make_metrics("b", err="0.4")], config)
        assert all("growth_potential" in r.unavailable_components for r in results)
        assert all(r.score is not None for r in results)

    def test_growth_is_used_when_available(self) -> None:
        config = make_config(weights={"growth_potential": 1.0})
        metrics = [
            make_metrics("shrinking", growth="-0.10"),
            make_metrics("growing", growth="0.50"),
        ]
        results = {r.slug: r for r in score_niches(metrics, config)}
        assert results["growing"].score > results["shrinking"].score

    def test_thin_niches_are_flagged_and_unscored(self) -> None:
        config = make_config(min_channels_for_scoring=3)
        results = {
            r.slug: r
            for r in score_niches(
                [make_metrics("thin", channels=2), make_metrics("solid", channels=9)], config
            )
        }
        assert results["thin"].insufficient_data is True
        assert results["thin"].score is None
        assert results["thin"].rank is None
        assert results["solid"].score is not None

    def test_thin_niches_do_not_affect_the_ranking_scale(self) -> None:
        config = make_config(weights={"engagement": 1.0}, min_channels_for_scoring=3)
        with_thin = score_niches(
            [
                make_metrics("a", err="0.10"),
                make_metrics("b", err="0.30"),
                make_metrics("thin", channels=1, err="0.99"),
            ],
            config,
        )
        without_thin = score_niches(
            [make_metrics("a", err="0.10"), make_metrics("b", err="0.30")], config
        )
        scores_with = {r.slug: r.score for r in with_thin if r.score is not None}
        scores_without = {r.slug: r.score for r in without_thin}
        assert scores_with == scores_without


class TestManualRatings:
    def test_defaults_are_marked_and_do_not_differentiate(self) -> None:
        config = make_config(weights={"brand_safety": 1.0})
        results = score_niches([make_metrics("a"), make_metrics("b")], config)
        assert all(r.score == 50.0 for r in results)
        assert all(r.uses_only_default_ratings for r in results)

    def test_override_changes_the_score(self) -> None:
        config = make_config(
            weights={"brand_safety": 1.0}, manual_overrides={"a": {"brand_safety": 90}}
        )
        results = {r.slug: r for r in score_niches([make_metrics("a"), make_metrics("b")], config)}
        assert results["a"].score == 90.0
        assert results["b"].score == 50.0
        assert results["a"].uses_only_default_ratings is False

    def test_inverted_rating_is_flipped(self) -> None:
        config = make_config(
            weights={"low_production_cost": 1.0},
            manual_overrides={"expensive": {"production_cost": 80}},
        )
        results = {
            r.slug: r
            for r in score_niches(
                [make_metrics("expensive"), make_metrics("cheap")], config
            )
        }
        assert results["expensive"].score == 20.0, "high production cost must lower the score"

    def test_components_are_serializable(self) -> None:
        result = score_niches([make_metrics("a")], make_config())[0]
        payload = result.components_dict()
        assert payload["engagement"]["source"] == "data"
        assert payload["brand_safety"]["source"] == "manual"
        assert payload["brand_safety"]["is_default_rating"] is True
