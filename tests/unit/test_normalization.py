"""Unit tests for value normalization rules."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from channel_factory.core.enums import Platform
from channel_factory.direct.normalization.rules import (
    ValueParseError,
    build_dedupe_key,
    clean_text,
    detect_platform,
    normalize_header,
    normalize_url,
    parse_date,
    parse_decimal,
    parse_int,
    parse_money,
    parse_percent,
    slugify,
)

# Written as escapes: these characters are invisible in source but common as
# thousands separators in Russian exports.
NBSP = "\u00a0"
NARROW_NBSP = "\u202f"
THIN_SPACE = "\u2009"


class TestParseInt:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("1240", 1240),
            ("1 240", 1240),
            (f"1{NBSP}240", 1240),
            (f"1{NARROW_NBSP}240", 1240),
            (f"1{THIN_SPACE}240", 1240),
            ("1,240", 1240),  # comma reads as thousands separator for counts
            ("1.240", 1240),  # so does a dot
            ("1240.0", 1240),
            ("1 234 567", 1234567),
            (1240, 1240),
            (1240.0, 1240),
        ],
    )
    def test_parses_counts(self, raw: object, expected: int) -> None:
        assert parse_int(raw) == expected

    @pytest.mark.parametrize("raw", [None, "", "   "])
    def test_blank_is_none(self, raw: object) -> None:
        assert parse_int(raw) is None

    @pytest.mark.parametrize("raw", ["abc", "12,5", "1 200 шт", True])
    def test_rejects_unparseable(self, raw: object) -> None:
        with pytest.raises(ValueParseError):
            parse_int(raw)


class TestParsePercent:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("24,7%", Decimal("0.247")),
            ("24.7%", Decimal("0.247")),
            ("0,247", Decimal("0.247")),
            (Decimal("0.247"), Decimal("0.247")),
        ],
    )
    def test_parses_without_warning(self, raw: object, expected: Decimal) -> None:
        value, warning = parse_percent(raw)
        assert value == expected
        assert warning is None

    def test_bare_number_above_one_is_percent_points_with_warning(self) -> None:
        value, warning = parse_percent("24.7")
        assert value == Decimal("0.247")
        assert warning is not None and "%" in warning

    def test_blank(self) -> None:
        assert parse_percent("") == (None, None)


class TestParseMoney:
    @pytest.mark.parametrize(
        ("raw", "amount", "currency"),
        [
            ("15 000 ₽", Decimal("15000"), "RUB"),
            ("15000 руб.", Decimal("15000"), "RUB"),
            ("1 500,50 ₽", Decimal("1500.50"), "RUB"),
            ("1,500.50 $", Decimal("1500.50"), "USD"),
            ("9500", Decimal("9500"), None),
        ],
    )
    def test_parses_amount_and_currency(
        self, raw: str, amount: Decimal, currency: str | None
    ) -> None:
        assert parse_money(raw) == (amount, currency)


class TestParseDecimal:
    def test_comma_is_decimal_separator_for_metrics(self) -> None:
        assert parse_decimal("1,25") == Decimal("1.25")

    def test_blank_is_none(self) -> None:
        assert parse_decimal("") is None


class TestParseDate:
    @pytest.mark.parametrize(
        "raw",
        ["2026-09-01", "01.09.2026", "01/09/2026", "2026/09/01", datetime(2026, 9, 1, 10, 30)],
    )
    def test_parses_known_formats(self, raw: object) -> None:
        assert parse_date(raw) == date(2026, 9, 1)

    def test_rejects_unknown_format(self) -> None:
        with pytest.raises(ValueParseError):
            parse_date("1 сентября 2026")


class TestNormalizeUrl:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            # Scheme and host are lowercased; the path keeps its original case,
            # because URL paths are case-sensitive in general. Case-insensitive
            # identity matching is build_dedupe_key's job, not this function's.
            ("https://t.me/AiTools/", "https://t.me/AiTools"),
            ("t.me/aitools", "https://t.me/aitools"),
            ("HTTPS://T.ME/AiTools", "https://t.me/AiTools"),
            ("https://max.ru/channel", "https://max.ru/channel"),
        ],
    )
    def test_normalizes(self, raw: str, expected: str) -> None:
        assert normalize_url(raw) == expected

    @pytest.mark.parametrize("raw", ["@aitools", "not a url", "-"])
    def test_rejects_non_urls(self, raw: str) -> None:
        with pytest.raises(ValueParseError):
            normalize_url(raw)

    def test_blank_is_none(self) -> None:
        assert normalize_url("") is None


class TestDetectPlatform:
    def test_explicit_label_wins(self) -> None:
        assert detect_platform("Телеграм", None) is Platform.TELEGRAM
        assert detect_platform("MAX", None) is Platform.MAX

    def test_falls_back_to_url_host(self) -> None:
        assert detect_platform(None, "https://t.me/x") is Platform.TELEGRAM
        assert detect_platform(None, "https://max.ru/x") is Platform.MAX

    def test_unknown_without_signals(self) -> None:
        assert detect_platform(None, None) is Platform.UNKNOWN
        assert detect_platform("vk", "https://example.com/x") is Platform.UNKNOWN


class TestHeadersAndSlugs:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Подписчики, чел.", "подписчики чел"),
            ("ERR %", "err"),
            ("  Название   канала ", "название канала"),
            ("Вовлечённость", "вовлеченность"),
        ],
    )
    def test_normalize_header(self, raw: str, expected: str) -> None:
        assert normalize_header(raw) == expected

    def test_slugify_transliterates(self) -> None:
        assert slugify("AI и нейросети") == "ai-i-neyroseti"

    def test_slugify_fallback(self) -> None:
        assert slugify("") == "unknown"

    def test_clean_text_collapses_whitespace(self) -> None:
        assert clean_text("  AI   Tools  ") == "AI Tools"


class TestDedupeKey:
    def test_external_id_has_priority(self) -> None:
        key = build_dedupe_key(external_id="12345", url="https://t.me/x", name="X")
        assert key == "ext:12345"

    def test_handle_beats_url(self) -> None:
        assert build_dedupe_key(handle="aitools", url="https://t.me/other") == "handle:aitools"

    def test_url_is_normalized(self) -> None:
        assert build_dedupe_key(url="https://T.me/AiTools/") == "url:t.me/aitools"

    def test_name_fallback(self) -> None:
        assert build_dedupe_key(name="AI Tools") == "name:ai-tools"

    def test_requires_any_identifier(self) -> None:
        with pytest.raises(ValueParseError):
            build_dedupe_key()
