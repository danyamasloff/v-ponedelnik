"""Finding the chat id of a channel the bot was added to.

Since June 2026 the API no longer offers ``GET /chats``, and the docs say the
list of chats must be assembled by the bot itself from events. Long polling
(``GET /updates``) is the way to do that without a public webhook endpoint, so
it is what the CLI uses to answer "which channel am I posting to?".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from channel_factory.core.logging import get_logger
from channel_factory.publishers.max.client import MaxApiClient

logger = get_logger(__name__)

#: Events that reveal a chat the bot belongs to.
DISCOVERY_TYPES = ["bot_added", "bot_started", "message_created", "message_created_in_channel"]


@dataclass(frozen=True)
class DiscoveredChat:
    """A chat id seen in an update, with whatever context came along."""

    chat_id: int
    update_type: str | None
    title: str | None = None


async def discover_chats(
    client: MaxApiClient,
    *,
    timeout: int = 30,
    marker: int | None = None,
) -> tuple[list[DiscoveredChat], int | None]:
    """Poll once for events and return the chat ids they mention.

    Returns the chats plus the next ``marker``; passing the marker back skips
    the events already seen.
    """
    response = await client.get_updates(marker=marker, timeout=timeout, types=DISCOVERY_TYPES)
    updates = response.get("updates") if isinstance(response, dict) else None
    next_marker = response.get("marker") if isinstance(response, dict) else None

    found: dict[int, DiscoveredChat] = {}
    for update in updates or []:
        if not isinstance(update, dict):
            continue
        update_type = update.get("update_type")
        for chat_id in _chat_ids(update):
            found.setdefault(
                chat_id,
                DiscoveredChat(chat_id=chat_id, update_type=update_type, title=_title(update)),
            )

    logger.info("max.discovery.poll", extra={"updates": len(updates or []), "chats": len(found)})
    return list(found.values()), next_marker if isinstance(next_marker, int) else None


def _chat_ids(node: Any) -> list[int]:
    """Collect every ``chat_id`` in an update.

    Different event types nest it differently (``bot_added.chat_id`` versus
    ``message_created.message.recipient.chat_id``), and the set of events can
    grow, so the value is looked up by name rather than by a fixed path.
    """
    found: list[int] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "chat_id" and isinstance(value, int):
                found.append(value)
            else:
                found.extend(_chat_ids(value))
    elif isinstance(node, list):
        for item in node:
            found.extend(_chat_ids(item))
    return found


def _title(update: dict[str, Any]) -> str | None:
    chat = update.get("chat")
    if isinstance(chat, dict):
        title = chat.get("title")
        if isinstance(title, str):
            return title
    return None
