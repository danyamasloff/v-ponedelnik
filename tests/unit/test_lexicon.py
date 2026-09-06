"""Unit tests for the deterministic relevance and practical-value lexicon."""

from __future__ import annotations

import pytest

from channel_factory.core.config import get_settings
from channel_factory.core.enums import EventType
from channel_factory.research.lexicon import LexiconError, load_topic_lexicon


@pytest.fixture(scope="module")
def lexicon():
    return load_topic_lexicon(get_settings().topic_lexicon_config)


class TestRelevance:
    def test_applied_ai_scores_high(self, lexicon) -> None:
        verdict = lexicon.relevance("How to train ChatGPT to write like you")
        assert verdict.value > 70
        assert verdict.matched

    def test_industry_news_scores_low(self, lexicon) -> None:
        verdict = lexicon.relevance("OpenAI closes a $40B funding round at a new valuation")
        assert verdict.value < 30
        assert verdict.penalised

    def test_applied_beats_corporate(self, lexicon) -> None:
        """The whole point: these two must not score the same."""
        applied = lexicon.relevance("Гайд: автоматизация отчётов в Excel с помощью нейросети")
        corporate = lexicon.relevance("Компания объявила о партнёрстве и назначении нового CEO")
        assert applied.value - corporate.value > 40

    def test_neutral_text_lands_near_the_base(self, lexicon) -> None:
        verdict = lexicon.relevance("Some entirely unrelated headline about weather")
        assert 30 <= verdict.value <= 50
        assert verdict.matched == ()

    def test_verdict_explains_itself(self, lexicon) -> None:
        """A number nobody can explain is not usable in an autonomous system."""
        verdict = lexicon.relevance("Промпт для Excel: автоматизация таблиц")
        explanation = verdict.explain()
        assert "промпт" in explanation
        assert explanation != "no lexicon terms matched"

    def test_values_stay_in_range(self, lexicon) -> None:
        piled_up = lexicon.relevance("промпт workflow excel автоматизац gemini copilot how to " * 5)
        assert 0 <= piled_up.value <= 100


class TestPracticalValue:
    def test_instructional_title_scores_high(self, lexicon) -> None:
        verdict = lexicon.practical_value(
            "How to automate your weekly report", None, EventType.GUIDE, has_version=False
        )
        assert verdict.value > 70

    def test_commentary_scores_low(self, lexicon) -> None:
        verdict = lexicon.practical_value(
            "Our thoughts on the future of the industry",
            None,
            EventType.OTHER,
            has_version=False,
        )
        assert verdict.value < 45

    def test_release_beats_commentary(self, lexicon) -> None:
        release = lexicon.practical_value(
            "Gemini 3.8 Flash is now available", None, EventType.RELEASE, has_version=True
        )
        commentary = lexicon.practical_value(
            "Reflections on the year", None, EventType.OTHER, has_version=False
        )
        assert release.value > commentary.value

    def test_body_counts_less_than_the_title(self, lexicon) -> None:
        in_title = lexicon.practical_value(
            "Инструкция по настройке", None, EventType.OTHER, has_version=False
        )
        in_body = lexicon.practical_value(
            "Заметка", "Инструкция по настройке", EventType.OTHER, has_version=False
        )
        assert in_title.value > in_body.value > 35


class TestLoading:
    def test_shipped_lexicon_is_valid(self, lexicon) -> None:
        assert lexicon.relevance_positive
        assert lexicon.relevance_negative
        assert lexicon.practical_patterns

    def test_missing_file(self, tmp_path) -> None:
        with pytest.raises(LexiconError, match="not found"):
            load_topic_lexicon(tmp_path / "nope.yaml")

    def test_unknown_event_type_rejected(self, tmp_path) -> None:
        path = tmp_path / "lex.yaml"
        path.write_text(
            "relevance:\n  positive:\n    ai: 5\n  negative:\n    ipo: 5\n"
            "practical_value:\n  patterns:\n    how to: 10\n"
            "  event_type_bonus:\n    NOT_A_TYPE: 5\n",
            encoding="utf-8",
        )
        with pytest.raises(LexiconError, match="unknown event types"):
            load_topic_lexicon(path)

    def test_negative_weight_rejected(self, tmp_path) -> None:
        path = tmp_path / "lex.yaml"
        path.write_text(
            "relevance:\n  positive:\n    ai: -5\n  negative:\n    ipo: 5\n"
            "practical_value:\n  patterns:\n    how to: 10\n",
            encoding="utf-8",
        )
        with pytest.raises(LexiconError, match="must not be negative"):
            load_topic_lexicon(path)
