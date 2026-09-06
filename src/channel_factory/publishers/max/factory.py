"""Building a MAX client and publisher from settings.

Kept apart from the client so the transport layer stays independent of how the
application happens to store its configuration.
"""

from __future__ import annotations

from pathlib import Path

from channel_factory.core.config import Settings, get_settings
from channel_factory.publishers.base import PublishError
from channel_factory.publishers.max.adapter import MaxPublisher
from channel_factory.publishers.max.client import MaxApiClient


class MaxConfigError(PublishError):
    """Required MAX settings are missing from the environment."""


def build_client(settings: Settings | None = None) -> MaxApiClient:
    """Client for the configured bot token."""
    settings = settings or get_settings()
    if not settings.max_bot_token:
        raise MaxConfigError(
            "MAX_BOT_TOKEN is not set. Create a bot in the MAX app (@MasterBot) "
            "and put its token into .env"
        )
    return MaxApiClient(
        settings.max_bot_token.get_secret_value(),
        base_url=settings.max_api_base_url,
        timeout=settings.max_request_timeout,
        ca_bundle=_ca_bundle(settings),
    )


def _ca_bundle(settings: Settings) -> Path | None:
    """Extra root CA for MAX requests, checked here so the error is readable."""
    bundle = settings.max_ca_bundle
    if bundle is None:
        return None
    if not bundle.is_file():
        raise MaxConfigError(f"MAX_CA_BUNDLE points to a missing file: {bundle}")
    return bundle


def resolve_chat_id(chat_id: int | None, settings: Settings | None = None) -> int:
    """Explicit chat id if given, otherwise the configured one."""
    settings = settings or get_settings()
    resolved = chat_id if chat_id is not None else settings.max_chat_id
    if resolved is None:
        raise MaxConfigError(
            "No channel id. Pass --chat-id or set MAX_CHAT_ID in .env "
            "(find it with the max-discover command)"
        )
    return resolved


def build_publisher(
    *,
    chat_id: int | None = None,
    rehearsal: bool = False,
    settings: Settings | None = None,
) -> tuple[MaxApiClient, MaxPublisher]:
    """Client plus publisher; the caller owns closing the client.

    ``rehearsal`` is for flows that will not send anything (a dry run, a
    preview). They must not demand a token or a channel id: the point is to
    look at the post before anything is configured, and the placeholder client
    is closed unused.
    """
    settings = settings or get_settings()
    if rehearsal and (settings.max_bot_token is None or settings.max_chat_id is None):
        token = settings.max_bot_token.get_secret_value() if settings.max_bot_token else "dry-run"
        client = MaxApiClient(
            token,
            base_url=settings.max_api_base_url,
            timeout=settings.max_request_timeout,
            ca_bundle=_ca_bundle(settings),
        )
        target = chat_id if chat_id is not None else settings.max_chat_id
        return client, MaxPublisher(client, target)

    client = build_client(settings)
    return client, MaxPublisher(client, resolve_chat_id(chat_id, settings))
