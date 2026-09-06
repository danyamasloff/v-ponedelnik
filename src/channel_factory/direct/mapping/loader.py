"""Column mapping: source headers -> canonical fields.

Export column names drift between files and between sources, so the mapping
lives in ``config/direct_columns.yaml`` rather than in code. Adding a new export
format must never require a code change.

An alias may also declare the **unit** its column is written in — real exports
put it in the header ("Просмотры, тыс", "ERR, %", "CPV, RUB"). The unit belongs
to the column, not to the individual value, which is why it is declared here and
not guessed per row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from channel_factory.direct.normalization.rules import normalize_header


class ColumnMappingError(Exception):
    """Raised when the mapping configuration itself is invalid."""


@dataclass(frozen=True)
class AliasSpec:
    """One accepted source header plus the unit it is written in."""

    header: str
    scale: int = 1
    percent_points: bool = False
    currency: str | None = None
    note: str | None = None

    @property
    def has_unit(self) -> bool:
        return self.scale != 1 or self.percent_points or self.currency is not None


DEFAULT_SPEC = AliasSpec(header="")


@dataclass(frozen=True)
class ColumnMappingConfig:
    """Canonical field -> accepted source header aliases."""

    fields: dict[str, tuple[AliasSpec, ...]]
    required: tuple[str, ...]

    def match(self, header: str) -> tuple[str, AliasSpec] | None:
        """Return ``(canonical_field, alias_spec)`` for a source header."""
        normalized = normalize_header(header)
        if not normalized:
            return None
        for canonical, aliases in self.fields.items():
            for alias in aliases:
                if alias.header == normalized:
                    return canonical, alias
        return None

    def canonical_for(self, header: str) -> str | None:
        """Return only the canonical field a source header maps to, if any."""
        matched = self.match(header)
        return None if matched is None else matched[0]


@dataclass(frozen=True)
class MappedColumns:
    """Result of matching a file's headers against the mapping config."""

    by_field: dict[str, int] = field(default_factory=dict)
    specs: dict[str, AliasSpec] = field(default_factory=dict)
    unmapped_headers: tuple[str, ...] = ()
    duplicate_headers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    missing_required: tuple[str, ...] = ()

    @property
    def is_usable(self) -> bool:
        """True when every required field was found."""
        return not self.missing_required

    def spec(self, canonical: str) -> AliasSpec:
        """Unit declaration for a mapped field, defaulting to "no unit"."""
        return self.specs.get(canonical, DEFAULT_SPEC)

    def declared_units(self) -> dict[str, str]:
        """Human-readable units per field, for the import record."""
        units: dict[str, str] = {}
        for canonical, spec in self.specs.items():
            parts = []
            if spec.scale != 1:
                parts.append(f"x{spec.scale}")
            if spec.percent_points:
                parts.append("percent points")
            if spec.currency:
                parts.append(spec.currency)
            if parts:
                units[canonical] = ", ".join(parts)
        return units


def _parse_alias(canonical: str, raw: object) -> AliasSpec:
    if isinstance(raw, str):
        return AliasSpec(header=normalize_header(raw))
    if not isinstance(raw, dict):
        raise ColumnMappingError(
            f"field '{canonical}': alias must be a string or a mapping, got {raw!r}"
        )
    header = raw.get("header")
    if not isinstance(header, str) or not header.strip():
        raise ColumnMappingError(f"field '{canonical}': alias mapping needs a 'header' string")
    scale = raw.get("scale", 1)
    if not isinstance(scale, int) or isinstance(scale, bool) or scale <= 0:
        raise ColumnMappingError(
            f"field '{canonical}', alias '{header}': 'scale' must be a positive integer"
        )
    percent_points = bool(raw.get("percent_points", False))
    currency = raw.get("currency")
    if currency is not None and not isinstance(currency, str):
        raise ColumnMappingError(
            f"field '{canonical}', alias '{header}': 'currency' must be a string"
        )
    return AliasSpec(
        header=normalize_header(header),
        scale=scale,
        percent_points=percent_points,
        currency=currency,
        note=raw.get("note"),
    )


def load_column_mapping(path: Path) -> ColumnMappingConfig:
    """Load and validate the column mapping configuration."""
    if not path.exists():
        raise ColumnMappingError(f"column mapping config not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_fields = raw.get("fields")
    if not isinstance(raw_fields, dict) or not raw_fields:
        raise ColumnMappingError(f"'fields' section is missing or empty in {path}")

    fields: dict[str, tuple[AliasSpec, ...]] = {}
    seen: dict[str, str] = {}
    for canonical, aliases in raw_fields.items():
        if not isinstance(aliases, list) or not aliases:
            raise ColumnMappingError(f"field '{canonical}' must list at least one alias")
        specs: list[AliasSpec] = []
        for alias in aliases:
            spec = _parse_alias(str(canonical), alias)
            if not spec.header:
                continue
            # Two fields claiming the same header would make mapping depend on
            # dictionary order, which is exactly the kind of silent ambiguity
            # this layer exists to prevent.
            owner = seen.get(spec.header)
            if owner is not None and owner != canonical:
                raise ColumnMappingError(
                    f"alias '{spec.header}' is claimed by both '{owner}' and '{canonical}'"
                )
            seen[spec.header] = str(canonical)
            specs.append(spec)
        fields[str(canonical)] = tuple(specs)

    required = tuple(str(item) for item in raw.get("required", []))
    unknown_required = [name for name in required if name not in fields]
    if unknown_required:
        raise ColumnMappingError(f"required fields not defined in 'fields': {unknown_required}")

    return ColumnMappingConfig(fields=fields, required=required)


def map_columns(headers: list[object], config: ColumnMappingConfig) -> MappedColumns:
    """Match file headers to canonical fields.

    The first occurrence of a field wins; later duplicates are reported so the
    import can warn instead of silently picking an arbitrary column.
    """
    by_field: dict[str, int] = {}
    specs: dict[str, AliasSpec] = {}
    duplicates: dict[str, list[str]] = {}
    unmapped: list[str] = []

    for index, header in enumerate(headers):
        text = str(header) if header is not None else ""
        matched = config.match(text)
        if matched is None:
            if normalize_header(text):
                unmapped.append(text.strip())
            continue
        canonical, spec = matched
        if canonical in by_field:
            duplicates.setdefault(canonical, []).append(text.strip())
            continue
        by_field[canonical] = index
        specs[canonical] = spec

    missing = tuple(name for name in config.required if name not in by_field)
    return MappedColumns(
        by_field=by_field,
        specs=specs,
        unmapped_headers=tuple(unmapped),
        duplicate_headers={key: tuple(value) for key, value in duplicates.items()},
        missing_required=missing,
    )
