"""Unit tests for column mapping."""

from __future__ import annotations

import pytest

from channel_factory.direct.mapping.loader import (
    AliasSpec,
    ColumnMappingConfig,
    ColumnMappingError,
    load_column_mapping,
    map_columns,
)


def aliases(*headers: str) -> tuple[AliasSpec, ...]:
    return tuple(AliasSpec(header=header) for header in headers)


@pytest.fixture
def config() -> ColumnMappingConfig:
    return ColumnMappingConfig(
        fields={
            "channel_name": aliases("канал", "название канала", "channel"),
            "subscribers": aliases("подписчики", "количество подписчиков", "subscribers"),
            "err": (
                AliasSpec(header="err"),
                AliasSpec(header="err %", percent_points=True),
            ),
        },
        required=("channel_name",),
    )


def test_maps_headers_to_field_indexes(config: ColumnMappingConfig) -> None:
    mapped = map_columns(["Канал", "Подписчики", "ERR"], config)
    assert mapped.by_field == {"channel_name": 0, "subscribers": 1, "err": 2}
    assert mapped.is_usable


def test_matching_ignores_case_punctuation_and_yo(config: ColumnMappingConfig) -> None:
    mapped = map_columns(["  КАНАЛ  ", "Количество подписчиков", "ERR"], config)
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


class TestUnits:
    def test_unit_declaration_follows_the_matched_alias(
        self, config: ColumnMappingConfig
    ) -> None:
        """"ERR, %" declares percent points; a bare "ERR" does not."""
        declared = map_columns(["Канал", "ERR, %"], config)
        bare = map_columns(["Канал", "ERR"], config)

        assert declared.spec("err").percent_points is True
        assert bare.spec("err").percent_points is False

    def test_missing_field_gets_a_neutral_spec(self, config: ColumnMappingConfig) -> None:
        mapped = map_columns(["Канал"], config)
        assert mapped.spec("predicted_views").scale == 1
        assert mapped.spec("predicted_views").percent_points is False

    def test_declared_units_are_reported(self) -> None:
        config = ColumnMappingConfig(
            fields={
                "channel_name": aliases("канал"),
                "predicted_views": (AliasSpec(header="просмотры тыс", scale=1000),),
                "cpv": (AliasSpec(header="cpv rub", currency="RUB"),),
            },
            required=(),
        )
        mapped = map_columns(["Канал", "Просмотры, тыс", "CPV, RUB"], config)
        assert mapped.declared_units() == {"predicted_views": "x1000", "cpv": "RUB"}


class TestLoadColumnMapping:
    def test_loads_project_config(self, column_mapping: ColumnMappingConfig) -> None:
        assert "channel_name" in column_mapping.fields
        assert column_mapping.required == ("channel_name",)
        assert column_mapping.canonical_for("Подписчики") == "subscribers"

    def test_real_export_headers_all_map(self, column_mapping: ColumnMappingConfig) -> None:
        """Headers taken verbatim from the real Yandex Direct export."""
        headers = [
            "Название канала",
            "Ссылка на канал",
            "Мессенджер",
            "Категория канала",
            "Количество подписчиков",
            "ERR, %",
            "Просмотры, тыс",
            "CPV, RUB",
            "Прогноз цены, RUB",
        ]
        mapped = map_columns(headers, column_mapping)

        assert mapped.unmapped_headers == ()
        assert mapped.is_usable
        assert set(mapped.by_field) == {
            "channel_name",
            "channel_url",
            "platform",
            "category",
            "subscribers",
            "err",
            "predicted_views",
            "cpv",
            "campaign_price",
        }
        assert mapped.spec("predicted_views").scale == 1000
        assert mapped.spec("err").percent_points is True
        assert mapped.spec("cpv").currency == "RUB"
        assert mapped.spec("campaign_price").currency == "RUB"

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

    def test_rejects_alias_claimed_by_two_fields(self, tmp_path) -> None:
        """Otherwise which field wins would depend on dictionary order."""
        path = tmp_path / "columns.yaml"
        path.write_text(
            "fields:\n"
            "  channel_name:\n    - Канал\n"
            "  category:\n    - Канал\n",
            encoding="utf-8",
        )
        with pytest.raises(ColumnMappingError, match="claimed by both"):
            load_column_mapping(path)

    def test_rejects_invalid_scale(self, tmp_path) -> None:
        path = tmp_path / "columns.yaml"
        path.write_text(
            "fields:\n"
            "  predicted_views:\n"
            "    - header: 'Просмотры, тыс'\n"
            "      scale: -5\n",
            encoding="utf-8",
        )
        with pytest.raises(ColumnMappingError, match="positive integer"):
            load_column_mapping(path)

    def test_rejects_alias_mapping_without_header(self, tmp_path) -> None:
        path = tmp_path / "columns.yaml"
        path.write_text(
            "fields:\n  predicted_views:\n    - scale: 1000\n",
            encoding="utf-8",
        )
        with pytest.raises(ColumnMappingError, match="needs a 'header' string"):
            load_column_mapping(path)
