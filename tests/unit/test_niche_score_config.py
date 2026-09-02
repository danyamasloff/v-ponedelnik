"""Unit tests for the niche score configuration loader."""

from __future__ import annotations

from pathlib import Path

import pytest

from channel_factory.niches.config import (
    MANUAL_RATING_KEYS,
    NicheScoreConfig,
    NicheScoreConfigError,
    load_niche_score_config,
)
from channel_factory.niches.scoring import ALL_COMPONENTS

VALID_CONFIG = """
score_version: test_v1
min_channels_for_scoring: 3
weights:
  engagement: 0.6
  brand_safety: 0.4
manual_defaults:
  content_scalability: 50
  production_cost: 50
  originality_potential: 50
  brand_safety: 50
  monetization_risk: 50
manual_overrides:
  ai-tech:
    brand_safety: 90
"""


def write_config(tmp_path: Path, content: str) -> Path:
    path = tmp_path / "niche_score.yaml"
    path.write_text(content, encoding="utf-8")
    return path


class TestLoading:
    def test_loads_valid_config(self, tmp_path: Path) -> None:
        config = load_niche_score_config(write_config(tmp_path, VALID_CONFIG))
        assert config.score_version == "test_v1"
        assert config.weights == {"engagement": 0.6, "brand_safety": 0.4}
        assert config.manual_rating("ai-tech", "brand_safety") == (90, True)
        assert config.manual_rating("other", "brand_safety") == (50, False)

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(NicheScoreConfigError, match="not found"):
            load_niche_score_config(tmp_path / "nope.yaml")

    def test_weights_must_sum_to_one(self, tmp_path: Path) -> None:
        content = VALID_CONFIG.replace("engagement: 0.6", "engagement: 0.9")
        with pytest.raises(NicheScoreConfigError, match=r"sum to 1\.0"):
            load_niche_score_config(write_config(tmp_path, content))

    def test_weights_section_required(self, tmp_path: Path) -> None:
        with pytest.raises(NicheScoreConfigError, match="weights"):
            load_niche_score_config(write_config(tmp_path, "score_version: x\n"))

    def test_rating_out_of_range(self, tmp_path: Path) -> None:
        content = VALID_CONFIG.replace("brand_safety: 90", "brand_safety: 140")
        with pytest.raises(NicheScoreConfigError, match="between 0 and 100"):
            load_niche_score_config(write_config(tmp_path, content))

    def test_unknown_override_key(self, tmp_path: Path) -> None:
        content = VALID_CONFIG.replace("brand_safety: 90", "brand_safery: 90")
        with pytest.raises(NicheScoreConfigError, match="unknown ratings"):
            load_niche_score_config(write_config(tmp_path, content))

    def test_missing_manual_default(self, tmp_path: Path) -> None:
        content = VALID_CONFIG.replace("  brand_safety: 50\n", "")
        with pytest.raises(NicheScoreConfigError, match="manual_defaults is missing"):
            load_niche_score_config(write_config(tmp_path, content))


class TestShippedConfig:
    def test_project_config_is_valid(self, niche_score_config: NicheScoreConfig) -> None:
        assert niche_score_config.score_version == "niche_score_v1"
        assert sum(niche_score_config.weights.values()) == pytest.approx(1.0)

    def test_every_component_has_a_weight(self, niche_score_config: NicheScoreConfig) -> None:
        """A component without a weight would silently never contribute."""
        missing = [
            spec.key for spec in ALL_COMPONENTS if spec.key not in niche_score_config.weights
        ]
        assert missing == []

    def test_no_weight_without_a_component(self, niche_score_config: NicheScoreConfig) -> None:
        """A weight without a component would silently do nothing."""
        known = {spec.key for spec in ALL_COMPONENTS}
        assert set(niche_score_config.weights) - known == set()

    def test_defaults_cover_every_manual_rating(
        self, niche_score_config: NicheScoreConfig
    ) -> None:
        assert set(niche_score_config.manual_defaults) == set(MANUAL_RATING_KEYS)
