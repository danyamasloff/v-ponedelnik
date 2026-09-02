"""Value-level normalization rules for market-data imports.

Design rules:

* Every function is pure and independently testable.
* Nothing is silently repaired: a value either parses, or raises
  :class:`ValueParseError`, or comes back with an explicit warning string.
* The caller decides whether a failure rejects the row or only warns.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

__all__ = [
    "ValueParseError",
    "build_dedupe_key",
    "clean_text",
    "detect_platform",
    "is_blank",
    "looks_like_handle",
    "normalize_handle",
    "normalize_header",
    "normalize_url",
    "parse_date",
    "parse_decimal",
    "parse_int",
    "parse_money",
    "parse_percent",
    "slugify",
    "to_decimal",
]

from channel_factory.core.enums import Platform


class ValueParseError(ValueError):
    """Raised when a source value cannot be parsed into the target type."""


# Unicode spaces used as thousands separators in Russian exports, written as
# escapes because they are invisible in source: NBSP, narrow NBSP, thin space,
# figure space, word joiner, zero-width space.
_SPACE_CHARS = "\u00a0\u202f\u2009\u2007\u2060\u200b"
_APOSTROPHES = "'\u2019\u02bc"

_TELEGRAM_HOSTS = frozenset({"t.me", "telegram.me", "telegram.dog"})
_MAX_HOSTS = frozenset({"max.ru"})

_TELEGRAM_LABELS = frozenset({"telegram", "телеграм", "телеграмм", "tg", "тг"})
_MAX_LABELS = frozenset({"max", "макс", "мах"})

# Longest tokens first so "руб." is matched before "руб".
_CURRENCY_TOKENS: tuple[tuple[str, str], ...] = (
    ("₽", "RUB"),
    ("руб.", "RUB"),
    ("rub", "RUB"),
    ("руб", "RUB"),
    ("$", "USD"),
    ("usd", "USD"),
    ("€", "EUR"),
    ("eur", "EUR"),
)

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ъ": "", "ы": "y",
    "ь": "", "э": "e", "ю": "yu", "я": "ya",
}

_DATE_FORMATS = (
    "%Y-%m-%d",
    "%d.%m.%Y",
    "%d/%m/%Y",
    "%Y/%m/%d",
    "%d-%m-%Y",
    "%d.%m.%y",
)


def is_blank(value: object) -> bool:
    """True for ``None`` and whitespace-only values."""
    return value is None or (isinstance(value, str) and not value.strip())


def clean_text(value: object) -> str | None:
    """Collapse whitespace and return ``None`` for blank values."""
    if is_blank(value):
        return None
    text = str(value)
    for char in _SPACE_CHARS:
        text = text.replace(char, " ")
    return re.sub(r"\s+", " ", text).strip() or None


def normalize_header(value: object) -> str:
    """Normalize a source column header for alias matching.

    Lowercases, unifies ``ё``/``е``, drops punctuation and collapses spaces, so
    ``"Подписчики, чел."`` and ``"подписчики чел"`` match the same alias.
    """
    text = clean_text(value) or ""
    text = text.lower().replace("ё", "е")
    text = re.sub(r"[.,;:()\[\]{}%\"'`«»№/\\|+*]", " ", text)
    text = re.sub(r"[–—−]", "-", text)
    return re.sub(r"\s+", " ", text).strip()


def _strip_numeric_noise(text: str) -> str:
    for char in _SPACE_CHARS:
        text = text.replace(char, "")
    for char in _APOSTROPHES:
        text = text.replace(char, "")
    return text.replace(" ", "")


def to_decimal(value: object, *, prefer_thousands_group: bool) -> Decimal:
    """Parse a number written in Russian or English notation.

    ``prefer_thousands_group`` resolves the genuinely ambiguous ``"1,240"``
    case: ``True`` reads it as 1240 (thousands separator, right for counts and
    prices), ``False`` reads it as 1.240 (decimal comma, right for ERR/CPV).
    """
    if isinstance(value, bool):
        raise ValueParseError(f"expected a number, got boolean: {value!r}")
    if isinstance(value, int | Decimal):
        return Decimal(value)
    if isinstance(value, float):
        return Decimal(str(value))

    text = _strip_numeric_noise(str(value).strip())
    if not text:
        raise ValueParseError("empty numeric value")
    if not re.fullmatch(r"[+-]?[\d.,]+", text):
        raise ValueParseError(f"not a number: {value!r}")

    has_comma, has_dot = "," in text, "." in text
    if has_comma and has_dot:
        decimal_sep = "," if text.rfind(",") > text.rfind(".") else "."
        thousands_sep = "." if decimal_sep == "," else ","
        text = text.replace(thousands_sep, "").replace(decimal_sep, ".")
    elif has_comma or has_dot:
        sep = "," if has_comma else "."
        parts = text.split(sep)
        if len(parts) > 2 or (prefer_thousands_group and len(parts[1]) == 3):
            text = text.replace(sep, "")
        else:
            text = text.replace(sep, ".")

    try:
        return Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - guarded by the regex above
        raise ValueParseError(f"not a number: {value!r}") from exc


def parse_int(value: object) -> int | None:
    """Parse an integer count (subscribers, views). Blank values return ``None``."""
    if is_blank(value):
        return None
    number = to_decimal(value, prefer_thousands_group=True)
    if number != number.to_integral_value():
        raise ValueParseError(f"expected a whole number, got {value!r}")
    return int(number)


def parse_decimal(value: object, *, prefer_thousands_group: bool = False) -> Decimal | None:
    """Parse a decimal metric (CPV and similar). Blank values return ``None``."""
    if is_blank(value):
        return None
    return to_decimal(value, prefer_thousands_group=prefer_thousands_group)


def parse_percent(value: object) -> tuple[Decimal | None, str | None]:
    """Parse a percentage into a fraction, returning ``(value, warning)``.

    ``"24,7%"`` and ``24.7`` both become ``0.247``; ``0.247`` stays ``0.247``.
    A value above 1 without a ``%`` sign is interpreted as percent points and
    reported through the warning, never silently.
    """
    if is_blank(value):
        return None, None
    text = str(value)
    has_percent_sign = "%" in text
    number = to_decimal(text.replace("%", ""), prefer_thousands_group=False)
    if has_percent_sign:
        return number / 100, None
    if number > 1:
        return number / 100, f"value {number} has no '%' sign; interpreted as percent points"
    return number, None


def parse_money(value: object) -> tuple[Decimal | None, str | None]:
    """Parse a monetary amount, returning ``(amount, currency_code)``."""
    if is_blank(value):
        return None, None
    text = str(value).strip().lower()
    currency: str | None = None
    for token, code in _CURRENCY_TOKENS:
        if token in text:
            currency = code
            text = text.replace(token, "")
    return to_decimal(text, prefer_thousands_group=True), currency


def parse_date(value: object) -> date | None:
    """Parse a date from a spreadsheet cell or a string in a known format."""
    if is_blank(value):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueParseError(f"unrecognized date format: {value!r}")


def looks_like_handle(value: object) -> bool:
    """True for ``@username`` style values."""
    text = clean_text(value)
    return bool(text and text.startswith("@") and len(text) > 1)


def normalize_handle(value: object) -> str | None:
    """Return a lowercase handle without the leading ``@``."""
    text = clean_text(value)
    if not text:
        return None
    return text.lstrip("@").lower() or None


def normalize_url(value: object) -> str | None:
    """Normalize a channel URL to ``scheme://host/path`` form.

    Raises :class:`ValueParseError` for handles and malformed values so the
    caller can record a data-quality warning instead of storing garbage.
    """
    text = clean_text(value)
    if not text:
        return None
    if text.startswith("@"):
        raise ValueParseError(f"value looks like a handle, not a URL: {value!r}")
    if not re.match(r"^https?://", text, re.IGNORECASE):
        if re.match(r"^[\w.-]+\.[a-z]{2,}(/|$)", text, re.IGNORECASE):
            text = f"https://{text}"
        else:
            raise ValueParseError(f"malformed URL: {value!r}")
    parsed = urlparse(text)
    if not parsed.netloc:
        raise ValueParseError(f"malformed URL: {value!r}")
    path = parsed.path.rstrip("/")
    return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{path}"


def detect_platform(explicit: object = None, url: str | None = None) -> Platform:
    """Resolve the platform from an explicit column value, then from the URL."""
    label = normalize_header(explicit)
    if label in _TELEGRAM_LABELS:
        return Platform.TELEGRAM
    if label in _MAX_LABELS:
        return Platform.MAX
    if url:
        host = urlparse(url).netloc.lower()
        if host in _TELEGRAM_HOSTS or any(host.endswith(f".{h}") for h in _TELEGRAM_HOSTS):
            return Platform.TELEGRAM
        if host in _MAX_HOSTS or any(host.endswith(f".{h}") for h in _MAX_HOSTS):
            return Platform.MAX
    return Platform.UNKNOWN


def slugify(value: object, *, max_length: int = 160) -> str:
    """Transliterate and slugify a label for use as a stable key."""
    text = (clean_text(value) or "").lower().replace("ё", "е")
    text = "".join(_TRANSLIT.get(char, char) for char in text)
    text = re.sub(r"[^a-z0-9]+", "-", text).strip("-")
    return text[:max_length] or "unknown"


def build_dedupe_key(
    *,
    external_id: str | None = None,
    handle: str | None = None,
    url: str | None = None,
    name: str | None = None,
) -> str:
    """Build the identity key used to recognise the same channel across imports.

    Preference order reflects how stable each identifier is: an explicit
    platform id beats a handle, which beats a URL, which beats the display name.
    """
    if external_id and external_id.strip():
        return f"ext:{external_id.strip().lower()}"
    if handle:
        return f"handle:{handle.lower()}"
    if url:
        parsed = urlparse(url)
        return f"url:{parsed.netloc.lower()}{parsed.path.rstrip('/').lower()}"
    if name and name.strip():
        return f"name:{slugify(name)}"
    raise ValueParseError("cannot build a dedupe key without any channel identifier")
