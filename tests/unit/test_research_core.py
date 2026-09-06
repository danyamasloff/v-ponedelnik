"""Unit tests for canonicalization, deduplication, scoring and pricing."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from channel_factory.core.enums import AiTaskType, ClusterStatus, EventType, MatchMethod, TrustLevel
from channel_factory.providers.llm.pricing import HAIKU, SONNET, estimate_cost, model_for
from channel_factory.research.canonical import (
    canonicalize_url,
    content_fingerprint,
    from_signed_64,
    hamming_distance,
    simhash,
    to_signed_64,
    tokenize,
    url_hash,
)
from channel_factory.research.config import (
    ResearchConfigError,
    load_source_configs,
    load_topic_score_config,
)
from channel_factory.research.dedup import (
    build_signature,
    detect_event_type,
    detect_vendor,
    detect_version,
    entity_cluster_key,
    match_cluster,
)
from channel_factory.research.scoring import ClusterScoreInput, score_cluster, timeliness_value

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


class TestCanonicalUrl:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("https://openai.com/news/", "https://openai.com/news"),
            ("https://WWW.OpenAI.com/News", "https://openai.com/News"),
            ("https://openai.com/news?utm_source=twitter", "https://openai.com/news"),
            ("https://openai.com/news?ref=hn&fbclid=x", "https://openai.com/news"),
            ("https://openai.com/news#section", "https://openai.com/news"),
            ("https://openai.com:443/news", "https://openai.com/news"),
            ("openai.com/news", "https://openai.com/news"),
        ],
    )
    def test_canonicalizes(self, raw: str, expected: str) -> None:
        assert canonicalize_url(raw) == expected

    def test_query_order_does_not_matter(self) -> None:
        first = canonicalize_url("https://example.com/a?b=2&a=1")
        second = canonicalize_url("https://example.com/a?a=1&b=2")
        assert first == second
        assert url_hash(first) == url_hash(second)

    def test_meaningful_query_is_kept(self) -> None:
        assert canonicalize_url("https://example.com/a?id=7") == "https://example.com/a?id=7"

    def test_empty_url_rejected(self) -> None:
        with pytest.raises(ValueError, match="empty url"):
            canonicalize_url("  ")


class TestFingerprint:
    def test_same_content_same_fingerprint(self) -> None:
        assert content_fingerprint("Title", "Summary") == content_fingerprint("Title", "Summary")

    def test_whitespace_and_case_do_not_matter(self) -> None:
        assert content_fingerprint("  TITLE ", "Body") == content_fingerprint("title", "body")

    def test_changed_content_changes_fingerprint(self) -> None:
        """This is what tells "already seen" from "the page was updated"."""
        before = content_fingerprint("Changelog", "v1 released")
        after = content_fingerprint("Changelog", "v2 released")
        assert before != after


class TestSimhash:
    def test_identical_texts_match(self) -> None:
        text = "OpenAI releases a new model for developers"
        assert hamming_distance(simhash(text), simhash(text)) == 0

    def test_similar_texts_are_close(self) -> None:
        left = simhash("Anthropic releases Claude Haiku 4.5 for developers today")
        right = simhash("Anthropic has released Claude Haiku 4.5 for developers")
        assert hamming_distance(left, right) < 20

    def test_different_texts_are_far(self) -> None:
        left = simhash("Anthropic releases Claude Haiku for developers")
        right = simhash("Пять лучших приложений для ведения дневника в 2026 году")
        assert hamming_distance(left, right) > 20

    def test_signed_roundtrip(self) -> None:
        value = simhash("some text that produces a large hash value")
        assert from_signed_64(to_signed_64(value)) == value

    def test_stopwords_are_dropped(self) -> None:
        assert "the" not in tokenize("The new release of the tool")


class TestEntityDetection:
    @pytest.mark.parametrize(
        ("text", "vendor"),
        [
            ("OpenAI ships a new API", "openai"),
            ("Claude gets a new feature", "anthropic"),
            ("Gemini 3.8 Flash is here", "google"),
            ("Microsoft 365 Copilot update", "microsoft"),
            ("Something unrelated entirely", None),
        ],
    )
    def test_vendor(self, text: str, vendor: str | None) -> None:
        assert detect_vendor(text) == vendor

    def test_version(self) -> None:
        assert detect_version("Claude Haiku 4.5 released") == "4.5"
        assert detect_version("no version here") is None

    def test_event_type(self) -> None:
        assert detect_event_type("Introducing Gemini 3.8") is EventType.RELEASE
        assert detect_event_type("How to use ChatGPT tasks") is EventType.GUIDE
        assert detect_event_type("Security incident report") is EventType.INCIDENT

    def test_entity_key_needs_more_than_a_vendor(self) -> None:
        """"OpenAI said something" must not become a cluster key of its own."""
        vague = _signature("OpenAI comments on the industry")
        assert entity_cluster_key(vague) is None

    def test_entity_key_groups_the_same_release(self) -> None:
        left = _signature("Introducing Gemini 3.8 Flash", at=NOW)
        right = _signature("Google launches Gemini 3.8 Flash today", at=NOW)
        assert entity_cluster_key(left) == entity_cluster_key(right)


_counter = iter(range(1, 10_000))


def _signature(title: str, *, summary: str = "", at: datetime = NOW, **kwargs):
    # A distinct URL per call: sharing one would make every pair match on the
    # URL layer and mask what the test is actually checking.
    unique = next(_counter)
    return build_signature(
        item_id=f"{title}-{unique}",
        url_hash=url_hash(canonicalize_url(f"https://example.com/{unique}")),
        content_fingerprint=content_fingerprint(title, summary),
        title=title,
        summary=summary,
        event_at=at,
        **kwargs,
    )


class TestMatching:
    def test_same_url_matches_first(self) -> None:
        first = _signature("Some release")
        second = build_signature(
            item_id="second",
            url_hash=first.url_hash,
            content_fingerprint="different",
            title="Completely different words here entirely",
            summary="",
            event_at=NOW,
        )
        matched = match_cluster(second, [first])
        assert matched is not None
        assert matched[1] is MatchMethod.URL

    def test_same_fingerprint_matches(self) -> None:
        first = _signature("Shared content", summary="body")
        second = build_signature(
            item_id="second",
            url_hash="other-hash",
            content_fingerprint=first.content_fingerprint,
            title="Shared content",
            summary="body",
            event_at=NOW,
        )
        matched = match_cluster(second, [first])
        assert matched is not None
        assert matched[1] is MatchMethod.FINGERPRINT

    def test_entity_matches_across_wording(self) -> None:
        first = _signature("Introducing Gemini 3.8 Flash")
        second = _signature("Google ships Gemini 3.8 Flash to everyone")
        matched = match_cluster(second, [first])
        assert matched is not None
        assert matched[1] is MatchMethod.ENTITY

    def test_unrelated_items_do_not_match(self) -> None:
        first = _signature("Introducing Gemini 3.8 Flash")
        second = _signature("Пять лучших приложений для заметок в 2026 году")
        assert match_cluster(second, [first]) is None

    def test_time_window_separates_repeated_events(self) -> None:
        first = _signature("Introducing Gemini 3.8 Flash", at=NOW)
        later = _signature("Introducing Gemini 3.8 Flash", at=NOW + timedelta(days=30))
        assert match_cluster(later, [first]) is None

    def test_different_versions_never_merge(self) -> None:
        """Consecutive releases share almost all of their changelog text."""
        body = "Bug fixes and improvements. " * 20
        first = _signature("stable", summary=body, version_hint="n8n@2.37.6")
        second = _signature("stable", summary=body, version_hint="n8n@2.37.7")
        assert match_cluster(second, [first]) is None

    def test_short_titles_skip_similarity_matching(self) -> None:
        first = _signature("stable")
        second = _signature("latest")
        assert match_cluster(second, [first]) is None

    def test_provider_version_hint_beats_the_title(self) -> None:
        signature = _signature("stable", version_hint="v2.37.7")
        assert signature.version == "2.37.7"


class TestTopicScore:
    @pytest.fixture
    def config(self):
        from channel_factory.core.config import get_settings

        return load_topic_score_config(get_settings().topic_score_config)

    def _input(self, **kwargs) -> ClusterScoreInput:
        defaults = {
            "cluster_key": "k",
            "event_type": EventType.RELEASE,
            "max_trust": TrustLevel.OFFICIAL,
            "event_at": NOW,
            "first_seen_at": NOW,
            "item_count": 2,
        }
        return ClusterScoreInput(**{**defaults, **kwargs})

    def test_scores_without_llm_and_renormalizes(self, config) -> None:
        result = score_cluster(self._input(), config, now=NOW)
        assert result.score is not None
        assert result.used_llm is False
        assert set(result.unavailable) == {
            "relevance",
            "practical_value",
            "audience_fit",
            "content_potential",
        }

    def test_llm_judgement_is_used_when_present(self, config) -> None:
        low = score_cluster(
            self._input(
                llm_judgement={
                    "relevance": 5,
                    "practical_value": 5,
                    "audience_fit": 5,
                    "content_potential": 5,
                }
            ),
            config,
            now=NOW,
        )
        high = score_cluster(
            self._input(
                llm_judgement={
                    "relevance": 95,
                    "practical_value": 95,
                    "audience_fit": 95,
                    "content_potential": 95,
                }
            ),
            config,
            now=NOW,
        )
        assert high.score > low.score
        assert high.used_llm is True
        assert high.unavailable == ()

    def test_thresholds_map_to_decisions(self, config) -> None:
        strong = score_cluster(self._input(), config, now=NOW)
        assert strong.decision is ClusterStatus.SELECTED

        stale = score_cluster(
            self._input(
                max_trust=TrustLevel.UNKNOWN,
                event_at=NOW - timedelta(days=20),
                first_seen_at=NOW - timedelta(days=20),
            ),
            config,
            now=NOW,
        )
        assert stale.decision is ClusterStatus.FILTERED
        assert stale.decision is not ClusterStatus.REJECTED, "a weak score is never final"

    def test_authority_follows_trust(self, config) -> None:
        official = score_cluster(self._input(max_trust=TrustLevel.OFFICIAL), config, now=NOW)
        unknown = score_cluster(self._input(max_trust=TrustLevel.UNKNOWN), config, now=NOW)
        assert official.score > unknown.score

    def test_timeliness_decays_by_half_life(self) -> None:
        fresh = timeliness_value(NOW, now=NOW, half_life_hours=48)
        one_half_life = timeliness_value(NOW - timedelta(hours=48), now=NOW, half_life_hours=48)
        assert fresh == pytest.approx(100.0)
        assert one_half_life == pytest.approx(50.0, abs=0.1)

    def test_already_published_topic_loses_novelty(self, config) -> None:
        fresh = score_cluster(self._input(), config, now=NOW)
        repeat = score_cluster(self._input(similar_to_published=True), config, now=NOW)
        assert repeat.score < fresh.score


class TestConfigLoading:
    def test_shipped_topic_config_is_valid(self) -> None:
        from channel_factory.core.config import get_settings

        config = load_topic_score_config(get_settings().topic_score_config)
        assert config.score_version == "topic_score_v1"
        assert sum(config.weights.values()) == pytest.approx(1.0)
        assert config.backlog_threshold < config.selected_threshold

    def test_shipped_sources_are_valid(self) -> None:
        from channel_factory.core.config import get_settings

        sources = load_source_configs(get_settings().research_sources_config)
        assert len(sources) >= 15
        assert all(source.reason for source in sources), "every source states why it is here"
        keys = [source.key for source in sources]
        assert len(keys) == len(set(keys))

    def test_weights_must_sum_to_one(self, tmp_path) -> None:
        path = tmp_path / "topic.yaml"
        path.write_text(
            "weights:\n  relevance: 0.5\n  practical_value: 0.1\n  novelty: 0.1\n"
            "  authority: 0.1\n  timeliness: 0.1\n  audience_fit: 0.1\n"
            "  content_potential: 0.5\n",
            encoding="utf-8",
        )
        with pytest.raises(ResearchConfigError, match=r"sum to 1\.0"):
            load_topic_score_config(path)

    def test_unknown_component_rejected(self, tmp_path) -> None:
        path = tmp_path / "topic.yaml"
        path.write_text("weights:\n  vibes: 1.0\n", encoding="utf-8")
        with pytest.raises(ResearchConfigError, match="unknown score components"):
            load_topic_score_config(path)

    def test_duplicate_source_key_rejected(self, tmp_path) -> None:
        path = tmp_path / "sources.yaml"
        path.write_text(
            "sources:\n"
            "  - key: a\n    provider: RSS\n    trust: OFFICIAL\n    url: https://e.com/f\n"
            "  - key: a\n    provider: RSS\n    trust: OFFICIAL\n    url: https://e.com/g\n",
            encoding="utf-8",
        )
        with pytest.raises(ResearchConfigError, match="duplicate source key"):
            load_source_configs(path)

    def test_github_source_requires_repo(self, tmp_path) -> None:
        path = tmp_path / "sources.yaml"
        path.write_text(
            "sources:\n  - key: a\n    provider: GITHUB_RELEASES\n    trust: OFFICIAL\n",
            encoding="utf-8",
        )
        with pytest.raises(ResearchConfigError, match="requires a repo"):
            load_source_configs(path)


class TestPricing:
    def test_routing_keeps_opus_out_of_the_pipeline(self) -> None:
        models = {model_for(task) for task in AiTaskType}
        assert models <= {HAIKU, SONNET}

    def test_cheap_tasks_use_the_cheap_model(self) -> None:
        assert model_for(AiTaskType.CLASSIFICATION) == HAIKU
        assert model_for(AiTaskType.SYNTHESIS) == SONNET

    def test_cost_estimate(self) -> None:
        cost = estimate_cost(HAIKU, input_tokens=1_000_000, output_tokens=0)
        assert cost == Decimal("1.000000")

    def test_cache_reads_are_cheaper_than_fresh_input(self) -> None:
        fresh = estimate_cost(SONNET, input_tokens=100_000, output_tokens=0)
        cached = estimate_cost(SONNET, input_tokens=0, output_tokens=0, cache_read_tokens=100_000)
        assert cached < fresh

    def test_unknown_model_is_an_error_not_free(self) -> None:
        """A silently free model would make the spend limits meaningless."""
        with pytest.raises(ValueError, match="no price known"):
            estimate_cost("claude-imaginary-9", input_tokens=1000, output_tokens=1000)
