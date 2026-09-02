"""Unit tests for column mapping."""

from __future__ import annotations

import pytest

from channel_factory.direct.mapping.loader import (
    ColumnMappingConfig,
    ColumnMappingError,
    load_column_mapping,
    map_columns,
)


@pytest.fixture
def config() -> ColumnMappingConfig:
    return ColumnMappingConfig(
        fields={
            "channel_name": ("канал", "название канала", "channel"),
            "subscribers": ("подписчики", "количество подписчиков", "subscribers"),
            "err": ("err", "вовлеченность"),
        },
        required=("channel_name",),
    )


def test_maps_headers_to_field_indexes(config: ColumnMappingConfig) -> None:
    mapped = map_columns(["Канал", "Подписчики", "ERR"], config)
    assert mapped.by_field == {"channel_name": 0, "subscribers": 1, "err": 2}
    assert mapped.is_usable


def test_matching_ignores_case_punctuation_and_yo(config: ColumnMappingConfig) -> None:
    mapped = map_columns(["  КАНАЛ  ", "Количество подписчиков", "Вовлечённость"], config)
    assert mapped.by_field == {"channel_name": 0, "subscribers": 1, "err": 2}


def test_unmapped_headers_are_reported(config: ColumnMappingConfig) -> None:
    mapped = map_columns(["Канал", "Регион", "Прогноз бюджета"], config)
    assert mapped.unmapped_headers == ("Регион", "Прогноз бюджета")


def test_missing_required_field_makes_mapping_unusable(config: ColumnMappingConfig) -> None:
    mapped = map_columns(["Подписчики", "ERR"], config)
    assert mapped.missing_required == ("channel_name",)
    assert not mapped.is_usable


def test_first_duplicate_wins_and_is_reported(config: ColumnMappingConfig) -> None:
    mapped = map_columns(["Канал", "Название канала"], config)
    assert mapped.by_field["channel_name"] == 0
    assert mapped.duplicate_headers == {"channel_name": ("Название канала",)}


def test_empty_headers_are_ignored(config: ColumnMappingConfig) -> None:
    mapped = map_columns(["Канал", "", None], config)
    assert mapped.unmapped_headers == ()


class TestLoadColumnMapping:
    def test_loads_project_config(self, column_mapping: ColumnMappingConfig) -> None:
        assert "channel_name" in column_mapping.fields
        assert column_mapping.required == ("channel_name",)
        assert column_mapping.canonical_for("Подписчики") == "subscribers"

    def test_missing_file(self, tmp_path) -> None:
        with pytest.raises(ColumnMappingError):
            load_column_mapping(tmp_path / "nope.yaml")

    def test_rejects_config_without_fields(self, tmp_path) -> None:
        path = tmp_path / "columns.yaml"
        path.write_text("version: 1\nrequired: []\n", encoding="utf-8")
        with pytest.raises(ColumnMappingError):
            load_column_mapping(path)

    def test_rejects_required_field_not_defined(self, tmp_path) -> None:
        path = tmp_path / "columns.yaml"
        path.write_text(
            "fields:\n  channel_name:\n    - Канал\nrequired:\n  - subscribers\n",
            encoding="utf-8",
        )
        with pytest.raises(ColumnMappingError):
            load_column_mapping(path)
