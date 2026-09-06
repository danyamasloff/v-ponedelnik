"""Unit tests for the audience-access rule and the own-posts track.

These decide what a Russian-speaking reader in MAX actually sees: which links
are printed at all, and whether a post we wrote ourselves is fit to publish.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from channel_factory.content import access
from channel_factory.content.drafting import render_draft
from channel_factory.content.evergreen import (
    EvergreenConfigError,
    EvergreenPost,
    EvergreenTopic,
    clean_body,
    load_topics,
)
from channel_factory.content.generation import GenerationError, clean_action
from channel_factory.core.enums import EventType, Platform
from channel_factory.publishers.scheduler import parse_own_slots

ANALYSIS = "Практический смысл в том, что рутину можно отдать модели. " * 3


class TestAudienceAccess:
    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://habr.com/ru/articles/1/", "habr.com"),
            ("https://www.cnews.ru/news", "cnews.ru"),
            ("https://blog.habr.com:443/x", "blog.habr.com"),
            (None, None),
        ],
    )
    def test_domain_normalisation(self, url: str | None, expected: str | None) -> None:
        assert access.normalize_domain(url) == expected

    def test_russian_sources_are_printable(self) -> None:
        assert access.audience_can_open("https://habr.com/ru/articles/900000/")

    def test_subdomains_of_a_russian_source_count(self) -> None:
        assert access.audience_can_open("https://m.habr.com/ru/articles/900000/")

    def test_foreign_sources_are_not(self) -> None:
        """A link the reader cannot open is worse than no link at all."""
        assert not access.audience_can_open("https://zapier.com/blog/train-chatgpt/")

    def test_unknown_domain_defaults_to_no(self) -> None:
        assert not access.audience_can_open("https://some-domain-we-never-declared.example/x")


class TestDraftLinks:
    def _draft(self, url: str, source: str):
        return render_draft(
            platform=Platform.MAX,
            cluster_id="c1",
            title="Заголовок",
            vendor="openai",
            product=None,
            version=None,
            event_type=EventType.GUIDE,
            primary_url=url,
            primary_source=source,
            analysis=ANALYSIS,
        )

    def test_reachable_source_gets_a_link(self) -> None:
        draft = self._draft("https://habr.com/ru/articles/1/", "Habr")
        assert "https://habr.com/ru/articles/1/" in draft.text
        assert draft.sources == ["https://habr.com/ru/articles/1/"]

    def test_unreachable_source_is_credited_without_a_url(self) -> None:
        draft = self._draft("https://zapier.com/blog/x", "Zapier Blog")
        assert "https://zapier.com" not in draft.text
        assert "Zapier Blog" in draft.text
        assert draft.sources == []

    def test_action_is_rendered_when_present(self) -> None:
        draft = render_draft(
            platform=Platform.MAX,
            cluster_id="c1",
            title="Заголовок",
            vendor=None,
            product=None,
            version=None,
            event_type=EventType.GUIDE,
            primary_url=None,
            primary_source=None,
            analysis=ANALYSIS,
            action="Перепишите один свой запрос по схеме роль-вход-ограничения-формат.",
        )
        assert "Что сделать: Перепишите" in draft.text


class TestOwnTopics:
    def test_the_shipped_backlog_loads(self) -> None:
        from channel_factory.core.config import get_settings

        topics = load_topics(get_settings().evergreen_topics_config)
        assert len(topics) >= 10
        assert len({topic.key for topic in topics}) == len(topics)
        assert all(topic.title for topic in topics)

    def test_missing_file_is_an_error(self, tmp_path: Path) -> None:
        with pytest.raises(EvergreenConfigError):
            load_topics(tmp_path / "nope.yaml")

    def test_topic_without_a_title_is_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "topics.yaml"
        path.write_text("topics:\n  - key: a\n", encoding="utf-8")
        with pytest.raises(EvergreenConfigError):
            load_topics(path)

    def test_duplicate_keys_are_refused(self, tmp_path: Path) -> None:
        path = tmp_path / "topics.yaml"
        path.write_text(
            "topics:\n  - key: a\n    title: A\n  - key: a\n    title: B\n", encoding="utf-8"
        )
        with pytest.raises(EvergreenConfigError):
            load_topics(path)

    def test_post_text_carries_headline_body_and_action(self) -> None:
        post = EvergreenPost(
            topic=EvergreenTopic(key="k", title="T"),
            headline="Заголовок",
            body="Тело поста.",
            action="Сделайте шаг.",
            backend="test",
            model="test",
        )
        assert post.text.startswith("**Заголовок**")
        assert "Тело поста." in post.text
        assert post.text.endswith("Что сделать: Сделайте шаг.")

    def test_a_post_without_an_action_still_renders(self) -> None:
        post = EvergreenPost(
            topic=EvergreenTopic(key="k", title="T"),
            headline="Заголовок",
            body="Тело поста.",
            action=None,
            backend="test",
            model="test",
        )
        assert "Что сделать" not in post.text


class TestBodyAndAction:
    def test_a_thin_body_is_refused(self) -> None:
        """Better no own post than a three-line one pretending to be a guide."""
        with pytest.raises(GenerationError):
            clean_body("Коротко и всё.")

    def test_a_long_body_is_cut_on_a_sentence(self) -> None:
        text = clean_body("Полезное предложение про работу с моделью. " * 80)
        assert text.endswith(".")
        assert len(text) <= 1800

    @pytest.mark.parametrize(
        "raw",
        ["", "https://example.com", "одно_слово", "x" * 300],
    )
    def test_useless_actions_are_dropped(self, raw: str) -> None:
        assert clean_action(raw) is None

    def test_a_real_action_survives(self) -> None:
        assert (
            clean_action('"Перепишите один запрос по схеме."') == "Перепишите один запрос по схеме."
        )


class TestOwnSlots:
    def test_parses_indexes(self) -> None:
        assert parse_own_slots("0,2") == frozenset({0, 2})

    def test_empty_means_no_own_slots(self) -> None:
        assert parse_own_slots("") == frozenset()

    def test_bad_value_is_refused(self) -> None:
        with pytest.raises(ValueError):
            parse_own_slots("середина")
