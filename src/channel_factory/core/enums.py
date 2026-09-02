"""Domain enums shared across modules.

All enum members use identical name and value so that the value stored in
PostgreSQL is stable and human-readable in raw SQL queries.
"""

from __future__ import annotations

from enum import StrEnum


class Platform(StrEnum):
    """Messenger platform a channel belongs to."""

    TELEGRAM = "TELEGRAM"
    MAX = "MAX"
    UNKNOWN = "UNKNOWN"


class SourceKind(StrEnum):
    """Where a batch of data originally came from."""

    DIRECT_EXPORT = "DIRECT_EXPORT"
    CATALOG = "CATALOG"
    CONTENT_REFERENCE = "CONTENT_REFERENCE"
    OTHER = "OTHER"


class ImportStatus(StrEnum):
    """Lifecycle status of a single import run.

    RUNNING is written before processing starts so a crashed import stays
    visible in the history instead of disappearing with the transaction.
    """

    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class RowStatus(StrEnum):
    """Per-row outcome of the import pipeline."""

    VALID = "VALID"
    REJECTED = "REJECTED"
