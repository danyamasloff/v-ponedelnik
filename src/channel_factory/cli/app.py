"""Typer application object shared by all command modules."""

from __future__ import annotations

import typer

app = typer.Typer(
    name="channel-factory",
    help="Market data, niche analytics and content pipeline for Telegram/MAX channels.",
    no_args_is_help=True,
    add_completion=False,
)
