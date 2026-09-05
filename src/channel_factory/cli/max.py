"""MAX publishing commands.

Every command talks to the MAX Bot API, which is free: no paid plan is needed
to create a bot or to post into a channel it administers.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import typer

from channel_factory.cli.app import app
from channel_factory.core.config import get_settings
from channel_factory.core.logging import setup_logging
from channel_factory.publishers.base import (
    MediaItem,
    Post,
    PostFormat,
    PublishError,
    PublishResult,
)
from channel_factory.publishers.max.adapter import MaxPreflight
from channel_factory.publishers.max.client import TEXT_LIMIT
from channel_factory.publishers.max.discovery import discover_chats
from channel_factory.publishers.max.factory import build_client, build_publisher, resolve_chat_id

PREVIEW_CHARS = 600


def _run[T](factory: Callable[[], Awaitable[T]]) -> T:
    """Run one async command, turning publishing errors into CLI failures."""
    settings = get_settings()
    setup_logging(settings.log_level)
    try:
        return asyncio.run(factory())
    except PublishError as exc:
        typer.secho(f"MAX error: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=1) from exc


@app.command("max-check")
def max_check(
    chat_id: int | None = typer.Option(None, "--chat-id", help="Channel id (default: MAX_CHAT_ID)"),
) -> None:
    """Check the bot token, the channel and the bot's posting rights."""
    preflight = _run(lambda: _preflight(chat_id))
    _print_preflight(preflight)
    if not preflight.ok:
        raise typer.Exit(code=1)


async def _preflight(chat_id: int | None) -> MaxPreflight:
    client, publisher = build_publisher(chat_id=chat_id)
    try:
        return await publisher.preflight()
    finally:
        await client.aclose()


@app.command("max-discover")
def max_discover(
    wait: int = typer.Option(30, "--wait", min=0, max=90, help="Long-poll seconds"),
) -> None:
    """Show chat ids from recent bot events (there is no list-chats method any more).

    Add the bot to the channel as an administrator, post something there, then
    run this command to learn the channel id.
    """
    chats = _run(lambda: _discover(wait))
    if not chats:
        typer.secho("NO DATA: no events in this poll.", fg=typer.colors.YELLOW)
        typer.echo(
            "Add the bot to the channel as an administrator or write a message there, "
            "then run the command again."
        )
        return
    typer.echo(f"{'CHAT ID':>20}  EVENT                 TITLE")
    for chat in chats:
        typer.echo(f"{chat.chat_id:>20}  {(chat.update_type or '-'):<20}  {chat.title or ''}")


async def _discover(wait: int) -> list[Any]:
    client = build_client()
    try:
        chats, _ = await discover_chats(client, timeout=wait)
        return chats
    finally:
        await client.aclose()


@app.command("max-post")
def max_post(
    text: str | None = typer.Argument(None, help="Post text (or use --text-file)"),
    text_file: Path | None = typer.Option(
        None, "--text-file", exists=True, dir_okay=False, help="Read the text from a UTF-8 file"
    ),
    chat_id: int | None = typer.Option(None, "--chat-id", help="Channel id (default: MAX_CHAT_ID)"),
    media: list[Path] = typer.Option(
        [], "--media", exists=True, dir_okay=False, help="Attach a local file (repeatable)"
    ),
    image_url: list[str] = typer.Option(
        [], "--image-url", help="Attach an image by URL (repeatable)"
    ),
    text_format: str = typer.Option(
        "plain", "--format", help="plain | markdown | html — how MAX renders the text"
    ),
    silent: bool = typer.Option(
        False, "--silent", help="Send without a notification (channels require notifications)"
    ),
    no_link_preview: bool = typer.Option(
        False, "--no-link-preview", help="Do not render a preview card for links in the text"
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Build and show the request without sending anything"
    ),
    skip_check: bool = typer.Option(
        False, "--skip-check", help="Skip the preflight check before posting"
    ),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation"),
) -> None:
    """Publish a post into a MAX channel."""
    post = _build_post(
        text=text,
        text_file=text_file,
        media=media,
        image_urls=image_url,
        text_format=text_format,
        silent=silent,
        no_link_preview=no_link_preview,
    )
    # A dry run is allowed before anything is configured; a real post is not.
    target = (
        (chat_id if chat_id is not None else get_settings().max_chat_id)
        if dry_run
        else resolve_chat_id(chat_id)
    )

    _print_post_preview(post, target, dry_run=dry_run)
    if not dry_run and not yes and not typer.confirm("Publish this post now?", default=False):
        typer.secho("Cancelled — nothing was published.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

    result = _run(lambda: _publish(post, target, dry_run=dry_run, skip_check=skip_check))
    _print_result(result)


async def _publish(
    post: Post, chat_id: int | None, *, dry_run: bool, skip_check: bool
) -> PublishResult:
    client, publisher = build_publisher(chat_id=chat_id, dry_run=dry_run)
    try:
        if not dry_run and not skip_check:
            preflight = await publisher.preflight()
            if not preflight.ok:
                _print_preflight(preflight)
                raise PublishError("preflight failed — nothing was published")
        return await publisher.publish(post)
    finally:
        await client.aclose()


@app.command("max-edit-post")
def max_edit_post(
    message_id: str = typer.Argument(..., help="Message id (mid) returned by max-post"),
    text: str | None = typer.Argument(None, help="New text (or use --text-file)"),
    text_file: Path | None = typer.Option(
        None, "--text-file", exists=True, dir_okay=False, help="Read the text from a UTF-8 file"
    ),
    chat_id: int | None = typer.Option(None, "--chat-id", help="Channel id (default: MAX_CHAT_ID)"),
    text_format: str = typer.Option("plain", "--format", help="plain | markdown | html"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation"),
) -> None:
    """Replace the text of a post the bot published earlier."""
    post = _build_post(
        text=text,
        text_file=text_file,
        media=[],
        image_urls=[],
        text_format=text_format,
        silent=False,
        no_link_preview=False,
    )
    target = resolve_chat_id(chat_id)
    _print_post_preview(post, target, dry_run=False)
    if not yes and not typer.confirm(f"Replace post {message_id}?", default=False):
        typer.secho("Cancelled — the post was left unchanged.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)

    _run(lambda: _edit(message_id, post, target))
    typer.secho(f"Edited   : {message_id}", fg=typer.colors.GREEN)


async def _edit(message_id: str, post: Post, chat_id: int) -> PublishResult:
    client, publisher = build_publisher(chat_id=chat_id)
    try:
        return await publisher.edit(message_id, post)
    finally:
        await client.aclose()


@app.command("max-delete-post")
def max_delete_post(
    message_id: str = typer.Argument(..., help="Message id (mid) returned by max-post"),
    chat_id: int | None = typer.Option(None, "--chat-id", help="Channel id (default: MAX_CHAT_ID)"),
    yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation"),
) -> None:
    """Delete a post the bot published earlier."""
    if not yes and not typer.confirm(f"Delete post {message_id}?", default=False):
        typer.secho("Cancelled — nothing was deleted.", fg=typer.colors.YELLOW)
        raise typer.Exit(code=1)
    _run(lambda: _delete(message_id, chat_id))
    typer.secho(f"Deleted  : {message_id}", fg=typer.colors.GREEN)


async def _delete(message_id: str, chat_id: int | None) -> None:
    client, publisher = build_publisher(chat_id=chat_id)
    try:
        await publisher.delete(message_id)
    finally:
        await client.aclose()


# ----------------------------------------------------------------- rendering


def _build_post(
    *,
    text: str | None,
    text_file: Path | None,
    media: list[Path],
    image_urls: list[str],
    text_format: str,
    silent: bool,
    no_link_preview: bool,
) -> Post:
    if text and text_file:
        typer.secho("Use either the text argument or --text-file, not both.", fg=typer.colors.RED)
        raise typer.Exit(code=2)

    body = text_file.read_text(encoding="utf-8") if text_file else (text or "")
    try:
        post_format = PostFormat(text_format.lower())
    except ValueError as exc:
        typer.secho(f"Unknown --format: {text_format}", fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc

    items = [MediaItem.from_path(path) for path in media]
    items += [MediaItem.from_url(url) for url in image_urls]

    try:
        return Post(
            text=body,
            media=tuple(items),
            format=post_format,
            notify=not silent,
            disable_link_preview=no_link_preview,
        )
    except PublishError as exc:
        typer.secho(f"Invalid post: {exc}", fg=typer.colors.RED)
        raise typer.Exit(code=2) from exc


def _print_post_preview(post: Post, chat_id: int | None, *, dry_run: bool) -> None:
    title = "DRY RUN — nothing will be sent" if dry_run else "About to publish"
    typer.secho(title, fg=typer.colors.BLUE, bold=True)
    typer.echo(f"Channel  : {chat_id if chat_id is not None else 'not configured (dry run)'}")
    typer.echo(f"Format   : {post.format.value}")
    typer.echo(f"Notify   : {'yes' if post.notify else 'no'}")
    if not post.notify:
        typer.secho(
            "Warning  : the API requires notify=true for channels; "
            "a silent post may be rejected.",
            fg=typer.colors.YELLOW,
        )
    typer.echo(f"Length   : {len(post.text)} / {TEXT_LIMIT} characters")
    for item in post.media:
        typer.echo(f"Media    : {item.kind.value}  {item.label}")
    typer.echo("--- text ---")
    typer.echo(post.text[:PREVIEW_CHARS] + ("…" if len(post.text) > PREVIEW_CHARS else ""))
    typer.echo("------------")


def _print_result(result: PublishResult) -> None:
    if result.dry_run:
        typer.secho("Dry run  : request built, nothing sent.", fg=typer.colors.YELLOW)
        body = (result.raw or {}).get("request_body", {})
        typer.echo(json.dumps(body, ensure_ascii=False, indent=2))
        return
    typer.secho("Published: OK", fg=typer.colors.GREEN)
    typer.echo(f"Chat id  : {result.chat_id}")
    typer.echo(f"Message  : {result.message_id or 'not returned by the API'}")
    if result.url:
        typer.echo(f"Link     : {result.url}")
    if result.published_at:
        typer.echo(f"Time     : {result.published_at:%Y-%m-%d %H:%M:%S} UTC")


def _print_preflight(preflight: MaxPreflight) -> None:
    bot = preflight.bot
    chat = preflight.chat
    typer.echo(f"Bot      : {bot.name or '?'} (@{bot.username or '?'}, id={bot.user_id})")
    typer.echo(f"Channel  : {chat.title or '?'} (id={chat.chat_id}, type={chat.type})")
    if chat.link:
        typer.echo(f"Link     : {chat.link}")
    if chat.participants_count is not None:
        typer.echo(f"Members  : {chat.participants_count}")
    typer.echo(f"Status   : {chat.status or '?'}")
    rights = ", ".join(preflight.permissions) if preflight.permissions else "-"
    typer.echo(f"Admin    : {_yes_no(preflight.is_admin)}  owner={_yes_no(preflight.is_owner)}")
    typer.echo(f"Rights   : {rights}")

    if preflight.ok:
        typer.secho("Can post : YES", fg=typer.colors.GREEN)
        return
    verdict = "NO" if preflight.can_post is False else "UNKNOWN"
    typer.secho(f"Can post : {verdict}", fg=typer.colors.RED)
    for problem in preflight.problems:
        typer.secho(f"  - {problem}", fg=typer.colors.RED)


def _yes_no(value: bool | None) -> str:
    if value is None:
        return "unknown"
    return "yes" if value else "no"
