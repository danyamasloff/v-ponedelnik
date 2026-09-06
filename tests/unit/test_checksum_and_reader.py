"""Unit tests for file checksums and table reading."""

from __future__ import annotations

from pathlib import Path

import pytest

from channel_factory.direct.importer.checksum import file_sha256
from channel_factory.direct.importer.reader import SourceFileError, read_table

EMPTY_FILE_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

HEADERS = ["Канал", "Ссылка", "Подписчики"]
ROWS = [
    ["AI Tools", "https://t.me/aitools", "12 500"],
    ["Neuro News", "https://t.me/neuronews", "8 000"],
]


class TestChecksum:
    def test_empty_file_has_known_digest(self, tmp_path: Path) -> None:
        path = tmp_path / "empty.bin"
        path.write_bytes(b"")
        assert file_sha256(path) == EMPTY_FILE_SHA256

    def test_same_content_same_digest(self, tmp_path: Path) -> None:
        first = tmp_path / "a.bin"
        second = tmp_path / "b.bin"
        first.write_bytes(b"market data")
        second.write_bytes(b"market data")
        assert file_sha256(first) == file_sha256(second)

    def test_different_content_different_digest(self, tmp_path: Path) -> None:
        first = tmp_path / "a.bin"
        second = tmp_path / "b.bin"
        first.write_bytes(b"market data")
        second.write_bytes(b"market data ")
        assert file_sha256(first) != file_sha256(second)


class TestReadTable:
    def test_reads_xlsx(self, xlsx_factory) -> None:
        table = read_table(xlsx_factory(HEADERS, ROWS))
        assert table.headers == HEADERS
        assert [row.values[0] for row in table.rows] == ["AI Tools", "Neuro News"]
        assert table.header_row_number == 1
        assert [row.number for row in table.rows] == [2, 3]

    def test_skips_title_rows_above_the_header(self, xlsx_factory) -> None:
        path = xlsx_factory(HEADERS, ROWS, title_rows=[["Отчёт по каналам"], []])
        table = read_table(path)
        assert table.headers == HEADERS
        assert table.header_row_number == 3
        assert [row.number for row in table.rows] == [4, 5]

    def test_skips_a_summary_block_of_label_value_pairs(self, xlsx_factory) -> None:
        """Real Yandex Direct exports open with a summary block before the table.

        Those rows have two non-empty cells each, so "first row with two cells"
        would pick the summary instead of the header.
        """
        summary = [
            ["Период публикации объявлений", "2026-09-02 - 2026-09-05"],
            ["Количество выбранных каналов", "11109"],
            ["Средний CPV, RUB", "0.92"],
            [],
        ]
        path = xlsx_factory(HEADERS, ROWS, title_rows=summary)
        table = read_table(path)

        assert table.headers == HEADERS
        assert table.header_row_number == 5
        assert [row.values[0] for row in table.rows] == ["AI Tools", "Neuro News"]

    def test_skips_fully_empty_rows(self, xlsx_factory) -> None:
        path = xlsx_factory(HEADERS, [ROWS[0], [None, None, None], ROWS[1]])
        table = read_table(path)
        assert len(table.rows) == 2

    def test_reads_semicolon_csv(self, csv_factory) -> None:
        table = read_table(csv_factory(HEADERS, ROWS))
        assert table.headers == HEADERS
        assert table.delimiter == ";"
        assert len(table.rows) == 2

    def test_reads_cp1251_csv(self, csv_factory) -> None:
        table = read_table(csv_factory(HEADERS, ROWS, encoding="cp1251", name="ru.csv"))
        assert table.headers == HEADERS
        assert table.encoding == "cp1251"

    def test_rejects_unsupported_suffix(self, tmp_path: Path) -> None:
        path = tmp_path / "data.json"
        path.write_text("{}", encoding="utf-8")
        with pytest.raises(SourceFileError, match="unsupported file type"):
            read_table(path)

    def test_rejects_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(SourceFileError, match="file not found"):
            read_table(tmp_path / "nope.xlsx")

    def test_rejects_file_without_header(self, csv_factory) -> None:
        path = csv_factory(["Канал"], [], name="single.csv")
        with pytest.raises(SourceFileError, match="no header row"):
            read_table(path)
