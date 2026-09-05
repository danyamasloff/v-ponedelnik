"""MAX publishing adapter (https://dev.max.ru/docs-api)."""

from channel_factory.publishers.max.adapter import (
    MaxBotInfo,
    MaxChatInfo,
    MaxPreflight,
    MaxPublisher,
)
from channel_factory.publishers.max.client import TEXT_LIMIT, MaxApiClient
from channel_factory.publishers.max.discovery import DiscoveredChat, discover_chats
from channel_factory.publishers.max.errors import (
    MaxApiError,
    MaxAuthError,
    MaxNotFoundError,
    MaxPermissionError,
    MaxRateLimitError,
    MaxServerError,
    MaxTransportError,
    MaxUploadError,
)
from channel_factory.publishers.max.factory import (
    MaxConfigError,
    build_client,
    build_publisher,
    resolve_chat_id,
)

__all__ = [
    "TEXT_LIMIT",
    "DiscoveredChat",
    "MaxApiClient",
    "MaxApiError",
    "MaxAuthError",
    "MaxBotInfo",
    "MaxChatInfo",
    "MaxConfigError",
    "MaxNotFoundError",
    "MaxPermissionError",
    "MaxPreflight",
    "MaxPublisher",
    "MaxRateLimitError",
    "MaxServerError",
    "MaxTransportError",
    "MaxUploadError",
    "build_client",
    "build_publisher",
    "discover_chats",
    "resolve_chat_id",
]
