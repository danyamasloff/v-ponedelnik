"""Guard against a crash class that only shows up when the code path runs.

``logging`` refuses to overwrite its own LogRecord attributes: passing
``extra={"created": ...}`` raises KeyError at the call site, not at import.
One such key shipped in the clustering path and crashed `research-cluster`
after a long poll, so the rule is checked statically for the whole package.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

SOURCE_ROOT = Path(__file__).resolve().parents[2] / "src"

RESERVED = set(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


def _collisions() -> list[str]:
    found: list[str] = []
    for path in SOURCE_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "extra" or not isinstance(keyword.value, ast.Dict):
                    continue
                for key in keyword.value.keys:
                    if isinstance(key, ast.Constant) and key.value in RESERVED:
                        found.append(
                            f"{path.relative_to(SOURCE_ROOT).as_posix()}:{node.lineno} "
                            f"uses reserved log field {key.value!r}"
                        )
    return found


def test_no_log_extra_shadows_a_logrecord_attribute() -> None:
    assert _collisions() == []
