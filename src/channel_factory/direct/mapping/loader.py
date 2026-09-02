"""Column mapping: source headers -> canonical fields.

Export column names drift between files and between sources, so the mapping
lives in ``config/direct_columns.yaml`` rather than in code. Adding a new alias
must never require a code change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from channel_factory.direct.normalization.rules import normalize_header


class ColumnMappingError(Exception):
    """Raised when the mapping configuration itself is invalid."""


@dataclass(frozen=True)
class ColumnMappingConfig:
    """Canonical field -> accepted (normalized) source header aliases."""

    fields: dict[str, tuple[str, ...]]
    required: tuple[str, ...]

    def canonical_for(self, header: str) -> str | None:
        """Return the canonical field a source header maps to, if any."""
        normalized = normalize_header(header)
        if not normalized:
            return None
        for canonical, aliases in self.fields.items():
            if normalized in aliases:
                return canonical
        return None


@dataclass(frozen=True)
class MappedColumns:
    """Result of matching a file's headers against the mapping config."""

    by_field: dict[str, int] = field(default_factory=dict)
    unmapped_headers: tuple[str, ...] = ()
    duplicate_headers: dict[str, tuple[str, ...]] = field(default_factory=dict)
    missing_required: tuple[str, ...] = ()

    @property
    def is_usable(self) -> bool:
        """True when every required field was found."""
        return not self.missing_required


def load_column_mapping(path: Path) -> ColumnMappingConfig:
    """Load and validate the column mapping configuration."""
    if not path.exists():
        raise ColumnMappingError(f"column mapping config not found: {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    raw_fields = raw.get("fields")
    if not isinstance(raw_fields, dict) or not raw_fields:
        raise ColumnMappingError(f"'fields' section is missing or empty in {path}")

    fields: dict[str, tuple[str, ...]] = {}
    for canonical, aliases in raw_fields.items():
        if not isinstance(aliases, list) or not aliases:
            raise ColumnMappingError(f"field '{canonical}' must list at least one alias")
        normalized = tuple(dict.fromkeys(normalize_header(alias) for alias in aliases))
        fields[str(canonical)] = tuple(alias for alias in normalized if alias)

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
    duplicates: dict[str, list[str]] = {}
    unmapped: list[str] = []

    for index, header in enumerate(headers):
        text = str(header) if header is not None else ""
        canonical = config.canonical_for(text)
        if canonical is None:
            if normalize_header(text):
                unmapped.append(text.strip())
            continue
        if canonical in by_field:
            duplicates.setdefault(canonical, []).append(text.strip())
            continue
        by_field[canonical] = index

    missing = tuple(name for name in config.required if name not in by_field)
    return MappedColumns(
        by_field=by_field,
        unmapped_headers=tuple(unmapped),
        duplicate_headers={key: tuple(value) for key, value in duplicates.items()},
        missing_required=missing,
    )
