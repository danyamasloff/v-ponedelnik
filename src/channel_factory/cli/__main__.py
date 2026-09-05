"""CLI entry point: ``python -m channel_factory.cli``."""

from __future__ import annotations

from channel_factory.cli import (  # noqa: F401  - registers commands on the app
    db,
    direct,
    max,
    niches,
)
from channel_factory.cli.app import app


def main() -> None:
    app()


if __name__ == "__main__":
    main()
