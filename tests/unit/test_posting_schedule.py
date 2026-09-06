"""Unit tests for slot arithmetic, card rendering and analysis cleanup.

These are the parts that decide *whether* and *what* we post, so they are
tested without a database or a network.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from pathlib import Path

import pytest

from channel_factory.content.cards import (
    CardContent,
    CardRenderError,
    CardStyle,
    accent_for,
    render_card,
    style_for,
)
from channel_factory.content.drafting import fact_line
from channel_factory.content.generation import (
    MAX_ANALYSIS_CHARS,
    AnalysisBrief,
    GenerationError,
    clean_analysis,
)
from channel_factory.content.identity import initials, render_avatar
from channel_factory.core.enums import EventType
from channel_factory.publishers.scheduler import (
    BreakingRules,
    open_slot,
    parse_slots,
    slots_for_day,
)

SLOTS = (time(9, 0), time(14, 0), time(19, 0))
WINDOW = timedelta(minutes=180)


def at(day: str, clock: str) -> datetime:
    """Local-time moment, the way the scheduler sees the clock."""
    return datetime.fromisoformat(f"{day}T{clock}").astimezone()


class TestParseSlots:
    def test_parses_and_sorts(self) -> None:
        assert parse_slots("19:00, 09:00,14:00") == SLOTS

    def test_duplicates_collapse(self) -> None:
        assert parse_slots("09:00,09:00") == (time(9, 0),)

    @pytest.mark.parametrize("spec", ["", "   ", ","])
    def test_empty_configuration_is_refused(self, spec: str) -> None:
        with pytest.raises(ValueError, match="no posting slots"):
            parse_slots(spec)

    @pytest.mark.parametrize("spec", ["9am", "25:00", "09-00"])
    def test_malformed_time_is_refused(self, spec: str) -> None:
        with pytest.raises(ValueError):
            parse_slots(spec)


class TestOpenSlot:
    def test_no_slot_before_the_first_one(self) -> None:
        assert open_slot(at("2026-09-06", "07:30"), SLOTS, WINDOW) is None

    def test_slot_opens_at_its_time(self) -> None:
        slot = open_slot(at("2026-09-06", "09:00"), SLOTS, WINDOW)
        assert slot is not None
        assert slot.starts_at.hour == 9

    def test_slot_stays_open_for_the_whole_window(self) -> None:
        """A runner that sleeps through 09:00 must still fill that slot."""
        slot = open_slot(at("2026-09-06", "11:45"), SLOTS, WINDOW)
        assert slot is not None
        assert slot.starts_at.hour == 9

    def test_slot_closes_after_the_window(self) -> None:
        assert open_slot(at("2026-09-06", "12:30"), SLOTS, WINDOW) is None

    def test_the_newer_slot_wins_when_windows_overlap(self) -> None:
        wide = timedelta(hours=8)
        slot = open_slot(at("2026-09-06", "15:00"), SLOTS, wide)
        assert slot is not None
        assert slot.starts_at.hour == 14

    def test_a_window_crossing_midnight_still_belongs_to_yesterday(self) -> None:
        late = (time(23, 0),)
        slot = open_slot(at("2026-09-07", "00:30"), late, timedelta(hours=3))
        assert slot is not None
        assert slot.starts_at.day == 6

    def test_three_slots_a_day(self) -> None:
        day = at("2026-09-06", "00:00")
        assert len(slots_for_day(day, SLOTS, WINDOW)) == 3


class TestCards:
    def test_renders_a_png(self, tmp_path: Path) -> None:
        path = render_card(
            CardContent(title="OpenAI обновила API", vendor="OpenAI", kicker="Релиз"),
            tmp_path / "card.png",
        )
        assert path.is_file()
        assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    def test_same_vendor_gets_the_same_accent(self) -> None:
        assert accent_for("Anthropic") == accent_for("anthropic")

    def test_different_vendors_can_differ(self) -> None:
        accents = {accent_for(name) for name in ("OpenAI", "Anthropic", "Google", "Meta")}
        assert len(accents) > 1

    def test_a_very_long_headline_still_renders(self, tmp_path: Path) -> None:
        path = render_card(
            CardContent(title="Очень длинный заголовок про нейросети " * 12),
            tmp_path / "long.png",
        )
        assert path.is_file()

    def test_empty_title_is_refused(self, tmp_path: Path) -> None:
        with pytest.raises(CardRenderError):
            render_card(CardContent(title="   "), tmp_path / "empty.png")


class TestCleanAnalysis:
    brief = AnalysisBrief(
        title="Anthropic выпустила Claude Opus 5",
        vendor="anthropic",
        product="Claude Opus 5",
        version=None,
        event_type=EventType.RELEASE,
    )

    def test_strips_quotes_and_fences(self) -> None:
        raw = '"' + "Практический смысл в том, что теперь можно короче. " * 3 + '"'
        assert not clean_analysis(raw, self.brief).startswith('"')

    def test_drops_the_headline_the_model_echoed_back(self) -> None:
        raw = "Anthropic выпустила Claude Opus 5. " + "Для читателя это значит вот что. " * 4
        cleaned = clean_analysis(raw, self.brief)
        assert not cleaned.lower().startswith("anthropic выпустила claude opus 5")

    def test_too_short_is_refused(self) -> None:
        """A one-liner is a failure, not a post: better no analysis than a stub."""
        with pytest.raises(GenerationError):
            clean_analysis("Ок.", self.brief)

    def test_too_long_is_cut_on_a_sentence_boundary(self) -> None:
        raw = "Это предложение про пользу для читателя. " * 40
        cleaned = clean_analysis(raw, self.brief)
        assert len(cleaned) <= MAX_ANALYSIS_CHARS
        assert cleaned.endswith(".")


class TestFactLine:
    """The one sentence we write ourselves must always read as Russian.

    Extraction sometimes yields a stray lowercase word as the "product"; a
    naive template then publishes "OpenAI обновил обновление" or
    "OpenAI опубликовал руководство scheduled".
    """

    def test_a_real_product_name_is_used(self) -> None:
        assert (
            fact_line(
                vendor="anthropic",
                product="Claude Opus 5",
                version=None,
                event_type=EventType.RELEASE,
            )
            == "Anthropic выпустил Claude Opus 5."
        )

    def test_version_is_appended_to_a_named_product(self) -> None:
        assert (
            fact_line(vendor="meta", product="Llama", version="4", event_type=EventType.RELEASE)
            == "Meta выпустил Llama 4."
        )

    def test_a_stray_lowercase_word_is_not_treated_as_a_product(self) -> None:
        assert (
            fact_line(
                vendor="openai", product="scheduled", version=None, event_type=EventType.GUIDE
            )
            == "OpenAI опубликовал руководство."
        )

    def test_transitive_phrase_gets_an_object_instead_of_dangling(self) -> None:
        assert (
            fact_line(
                vendor="openai", product="automation", version="5.6", event_type=EventType.UPDATE
            )
            == "OpenAI выпустил обновление."
        )

    def test_no_vendor_means_no_sentence(self) -> None:
        assert (
            fact_line(vendor=None, product="X", version=None, event_type=EventType.RELEASE) is None
        )


class TestBreakingRules:
    """The bar for interrupting the schedule.

    Every condition here exists to make BREAKING rare: a channel that shouts
    daily is a feed, and "breaking" that turns out routine costs more trust
    than a late post.
    """

    def test_defaults_demand_confirmation(self) -> None:
        rules = BreakingRules()
        assert rules.min_sources >= 2
        assert rules.min_score >= 90
        assert rules.max_per_day <= 3

    def test_a_gap_between_posts_is_enforced_by_default(self) -> None:
        assert BreakingRules().min_gap >= timedelta(minutes=30)

    def test_it_can_be_switched_off_entirely(self) -> None:
        assert BreakingRules(enabled=False).enabled is False


class TestCardStyles:
    """Every post gets a layout, and the same post always gets the same one."""

    def test_all_five_styles_render(self, tmp_path: Path) -> None:
        content = CardContent(
            title="Cursor перестал работать у пользователей из России",
            vendor="cursor",
            kicker="рынок",
            footer="В понедельник · практические навыки",
            brand="В понедельник",
        )
        for style in CardStyle:
            path = render_card(content, tmp_path / f"{style.value}.png", style=style)
            assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

    def test_style_is_stable_for_a_seed(self) -> None:
        assert style_for("cluster-1") is style_for("cluster-1")

    def test_different_posts_get_different_styles(self) -> None:
        """Not a guarantee for any two posts — a guarantee of variety overall."""
        seeds = [f"cluster-{index}" for index in range(40)]
        assert len({style_for(seed) for seed in seeds}) == len(CardStyle)

    def test_style_survives_a_missing_date(self, tmp_path: Path) -> None:
        """The date block falls back to today rather than crashing."""
        path = render_card(
            CardContent(title="Заголовок без даты"),
            tmp_path / "no-date.png",
            style=CardStyle.ACCENT_BLOCK,
        )
        assert path.is_file()


class TestIdentity:
    def test_initials_of_a_two_word_name(self) -> None:
        assert initials("В понедельник") == "Вп"

    def test_initials_of_a_single_word(self) -> None:
        assert initials("Понедельник") == "По"

    def test_empty_name_is_refused(self) -> None:
        with pytest.raises(CardRenderError):
            initials("   ")

    def test_avatar_is_square(self, tmp_path: Path) -> None:
        from PIL import Image

        path = render_avatar("В понедельник", tmp_path / "avatar.png")
        with Image.open(path) as image:
            assert image.size[0] == image.size[1]
