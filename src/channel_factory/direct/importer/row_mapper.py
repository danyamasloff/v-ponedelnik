"""Turning one raw source row into a validated, normalized record.

Failure policy (deliberate, see docs/direct-import.md):

* Missing channel identity -> the row is REJECTED (it cannot be attributed).
* Any other unparseable or impossible value -> WARNING, the field is stored as
  ``NULL`` and the raw value is preserved. One bad cell never costs a row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from channel_factory.core.enums import Platform
from channel_factory.direct.importer.reader import SourceRow
from channel_factory.direct.mapping.loader import MappedColumns
from channel_factory.direct.normalization.rules import (
    ValueParseError,
    build_dedupe_key,
    clean_text,
    detect_platform,
    looks_like_handle,
    normalize_handle,
    normalize_url,
    parse_date,
    parse_decimal,
    parse_int,
    parse_money,
    parse_percent,
)

# Metrics that must never be negative; a negative value is recorded as a data
# quality warning and stored as NULL rather than as a fact.
_NON_NEGATIVE_FIELDS = ("subscribers", "predicted_views", "cpv", "campaign_price")

# ERR above this is suspicious but not impossible (views can exceed subscribers).
_ERR_WARNING_THRESHOLD = Decimal(5)


def jsonable(value: Any) -> Any:
    """Convert a spreadsheet value into something JSONB can store losslessly."""
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, datetime | date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return str(value)


@dataclass
class NormalizedRow:
    """Outcome of normalizing a single source row."""

    row_number: int
    raw: dict[str, Any] = field(default_factory=dict)
    normalized: dict[str, Any] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    channel_name: str | None = None
    channel_url: str | None = None
    external_id: str | None = None
    handle: str | None = None
    platform: Platform = Platform.UNKNOWN
    dedupe_key: str | None = None
    category: str | None = None

    snapshot_date: date | None = None
    subscribers: int | None = None
    err: Decimal | None = None
    predicted_views: int | None = None
    cpv: Decimal | None = None
    campaign_price: Decimal | None = None
    currency: str | None = None
    region: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return not self.errors


def _raw_dict(headers: list[str], values: list[Any]) -> dict[str, Any]:
    """Header -> raw value for every column, including unmapped ones."""
    raw: dict[str, Any] = {}
    for index, value in enumerate(values):
        key = headers[index] if index < len(headers) and headers[index] else f"column_{index + 1}"
        raw[key] = jsonable(value)
    return raw


def normalize_row(
    source_row: SourceRow,
    headers: list[str],
    mapped: MappedColumns,
    *,
    default_snapshot_date: date,
) -> NormalizedRow:
    """Normalize and validate one row."""
    row = NormalizedRow(row_number=source_row.number)
    row.raw = _raw_dict(headers, source_row.values)

    def cell(field_name: str) -> Any:
        index = mapped.by_field.get(field_name)
        if index is None or index >= len(source_row.values):
            return None
        return source_row.values[index]

    def parse(field_name: str, parser: Any) -> Any:
        """Run a parser, downgrading failures to warnings."""
        try:
            return parser(cell(field_name))
        except ValueParseError as exc:
            row.warnings.append(f"{field_name}: {exc}")
            return None

    row.channel_name = clean_text(cell("channel_name"))
    if not row.channel_name:
        row.errors.append("channel_name: value is empty")

    raw_url = cell("channel_url")
    try:
        row.channel_url = normalize_url(raw_url)
    except ValueParseError as exc:
        if looks_like_handle(raw_url):
            row.handle = normalize_handle(raw_url)
            row.warnings.append(f"channel_url: treated as handle {row.handle!r}")
        else:
            row.warnings.append(f"channel_url: {exc}")

    row.external_id = clean_text(cell("channel_external_id"))
    row.platform = detect_platform(cell("platform"), row.channel_url)
    row.category = clean_text(cell("category"))
    row.region = clean_text(cell("region"))

    row.subscribers = parse("subscribers", parse_int)
    row.predicted_views = parse("predicted_views", parse_int)
    row.cpv = parse("cpv", lambda value: parse_decimal(value, prefer_thousands_group=False))

    err_result = parse("err", parse_percent)
    if err_result is not None:
        row.err, err_warning = err_result
        if err_warning:
            row.warnings.append(f"err: {err_warning}")
        if row.err is not None and row.err > _ERR_WARNING_THRESHOLD:
            row.warnings.append(f"err: suspiciously high value {row.err}")

    money_result = parse("campaign_price", parse_money)
    if money_result is not None:
        row.campaign_price, detected_currency = money_result
        row.currency = clean_text(cell("currency")) or detected_currency

    parsed_date = parse("snapshot_date", parse_date)
    if parsed_date is None:
        row.snapshot_date = default_snapshot_date
        if mapped.by_field.get("snapshot_date") is not None and not row.warnings:
            row.warnings.append("snapshot_date: empty, using the import default")
    else:
        row.snapshot_date = parsed_date

    for field_name in _NON_NEGATIVE_FIELDS:
        value = getattr(row, field_name)
        if value is not None and value < 0:
            row.warnings.append(f"{field_name}: negative value {value} discarded")
            setattr(row, field_name, None)

    if row.channel_name or row.external_id or row.handle or row.channel_url:
        try:
            row.dedupe_key = build_dedupe_key(
                external_id=row.external_id,
                handle=row.handle,
                url=row.channel_url,
                name=row.channel_name,
            )
        except ValueParseError as exc:
            row.errors.append(f"dedupe_key: {exc}")

    row.extra = {
        header: value
        for header, value in row.raw.items()
        if header in mapped.unmapped_headers and value is not None
    }
    row.normalized = {
        "channel_name": row.channel_name,
        "channel_url": row.channel_url,
        "external_id": row.external_id,
        "handle": row.handle,
        "platform": row.platform.value,
        "dedupe_key": row.dedupe_key,
        "category": row.category,
        "region": row.region,
        "snapshot_date": row.snapshot_date.isoformat() if row.snapshot_date else None,
        "subscribers": row.subscribers,
        "err": str(row.err) if row.err is not None else None,
        "predicted_views": row.predicted_views,
        "cpv": str(row.cpv) if row.cpv is not None else None,
        "campaign_price": str(row.campaign_price) if row.campaign_price is not None else None,
        "currency": row.currency,
    }
    return row
