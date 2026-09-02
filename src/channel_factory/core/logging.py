"""Structured-ish logging setup.

Log records carry structured fields via the ``extra`` dict; the formatter
appends them as ``key=value`` pairs. Never pass secrets (tokens, passwords,
raw database URLs) as fields — use masked values.
"""

from __future__ import annotations

import logging
import sys
from typing import Any

_RESERVED = frozenset(logging.LogRecord("", 0, "", 0, "", None, None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class KeyValueFormatter(logging.Formatter):
    """``timestamp level logger | message | key=value`` formatter."""

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        fields = {k: v for k, v in record.__dict__.items() if k not in _RESERVED}
        if not fields:
            return base
        rendered = " ".join(f"{key}={_render(value)}" for key, value in sorted(fields.items()))
        return f"{base} | {rendered}"


def _render(value: Any) -> str:
    text = str(value)
    return f'"{text}"' if " " in text else text


def setup_logging(level: str = "INFO") -> None:
    """Configure root logging once, writing to stderr."""
    root = logging.getLogger()
    if root.handlers:
        root.setLevel(level.upper())
        return
    handler = logging.StreamHandler(stream=sys.stderr)
    handler.setFormatter(
        KeyValueFormatter(
            fmt="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S%z",
        )
    )
    root.addHandler(handler)
    root.setLevel(level.upper())


def get_logger(name: str) -> logging.Logger:
    """Return a module-level logger."""
    return logging.getLogger(name)
