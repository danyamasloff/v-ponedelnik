"""MAX publishing adapter: domain post in, platform result out.

The adapter owns the rules that belong to publishing rather than to HTTP:
what a valid post is, how media becomes attachments, what a channel must look
like before we send anything, and how the platform answer maps back to a
:class:`PublishResult`.

Deliberately stateless — nothing is written to PostgreSQL here. Publication
history is a later phase and gets its own schema then.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from channel_factory.core.enums import Platform
from channel_factory.core.logging import get_logger
from channel_factory.publishers.base import (
    Post,
    PostFormat,
    PostValidationError,
    PublishResult,
)
from channel_factory.publishers.max.client import (
    TEXT_LIMIT,
    MaxApiClient,
    is_attachment_not_ready,
)
from channel_factory.publishers.max.errors import MaxApiError

logger = get_logger(__name__)

#: Chat types that accept posts from a bot. A "dialog" is a private chat with
#: one user and is never a publishing target here.
PUBLISHABLE_CHAT_TYPES = frozenset({"channel", "chat"})

#: Admin rights that let a bot publish. The API returns a permission list;
#: owners and admins may come back with an empty one, hence the flags too.
POSTING_PERMISSIONS = frozenset({"write", "post_edit_delete_message", "edit_message", "edit"})


@dataclass(frozen=True)
class MaxBotInfo:
    """Answer of GET /me, reduced to what the CLI shows."""

    user_id: int | None
    name: str | None
    username: str | None
    is_bot: bool

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> MaxBotInfo:
        return cls(
            user_id=data.get("user_id"),
            name=data.get("first_name") or data.get("name"),
            username=data.get("username"),
            is_bot=bool(data.get("is_bot")),
        )


@dataclass(frozen=True)
class MaxChatInfo:
    """Answer of GET /chats/{chatId}, reduced to what the CLI shows."""

    chat_id: int | None
    type: str | None
    status: str | None
    title: str | None
    link: str | None
    description: str | None
    is_public: bool | None
    participants_count: int | None
    messages_count: int | None

    @classmethod
    def from_api(cls, data: dict[str, Any]) -> MaxChatInfo:
        return cls(
            chat_id=data.get("chat_id"),
            type=data.get("type"),
            status=data.get("status"),
            title=data.get("title"),
            link=data.get("link"),
            description=data.get("description"),
            is_public=data.get("is_public"),
            participants_count=data.get("participants_count"),
            messages_count=data.get("messages_count"),
        )


@dataclass(frozen=True)
class MaxPreflight:
    """Everything checked before the first post.

    ``can_post`` is ``None`` when the platform did not let us find out — an
    unknown answer is reported as unknown instead of being read as "yes".
    """

    bot: MaxBotInfo
    chat: MaxChatInfo
    is_admin: bool | None
    is_owner: bool | None
    permissions: tuple[str, ...]
    can_post: bool | None
    problems: tuple[str, ...] = field(default_factory=tuple)

    @property
    def ok(self) -> bool:
        return not self.problems and self.can_post is not False


class MaxPublisher:
    """Publishes posts into one MAX channel or group chat."""

    platform = Platform.MAX

    def __init__(
        self,
        client: MaxApiClient,
        chat_id: int,
        *,
        dry_run: bool = False,
        attachment_attempts: int = 4,
        attachment_retry_delay: float = 5.0,
    ) -> None:
        self._client = client
        self._chat_id = chat_id
        self._dry_run = dry_run
        self._attachment_attempts = max(1, attachment_attempts)
        self._attachment_retry_delay = attachment_retry_delay

    @property
    def chat_id(self) -> int:
        return self._chat_id

    # ------------------------------------------------------------- preflight

    async def preflight(self) -> MaxPreflight:
        """Check token, chat and posting rights before anything is published."""
        bot = MaxBotInfo.from_api(await self._client.get_me())
        chat = MaxChatInfo.from_api(await self._client.get_chat(self._chat_id))

        problems: list[str] = []
        if chat.type not in PUBLISHABLE_CHAT_TYPES:
            problems.append(
                f"chat type {chat.type!r} is not a channel or group chat — nothing to post to"
            )
        if chat.status and chat.status != "active":
            problems.append(f"chat status is {chat.status!r}, not 'active'")

        is_admin: bool | None = None
        is_owner: bool | None = None
        permissions: tuple[str, ...] = ()
        can_post: bool | None = None
        try:
            membership = await self._client.get_my_membership(self._chat_id)
        except MaxApiError as exc:
            # 403/404 here usually means the bot is not an administrator, but
            # the API does not promise that, so it is reported, not assumed.
            problems.append(f"could not read bot membership: {exc}")
        else:
            is_admin = bool(membership.get("is_admin"))
            is_owner = bool(membership.get("is_owner"))
            raw_permissions = membership.get("permissions") or []
            permissions = tuple(str(item) for item in raw_permissions)
            can_post = bool(
                is_owner or is_admin or (set(permissions) & POSTING_PERMISSIONS)
            )
            if not can_post:
                problems.append(
                    "bot is a member but has no posting rights — "
                    "make it an administrator of the channel"
                )

        return MaxPreflight(
            bot=bot,
            chat=chat,
            is_admin=is_admin,
            is_owner=is_owner,
            permissions=permissions,
            can_post=can_post,
            problems=tuple(problems),
        )

    # --------------------------------------------------------------- publish

    async def publish(self, post: Post) -> PublishResult:
        """Send one post. In dry-run mode nothing leaves the machine."""
        _validate(post)

        if self._dry_run:
            body = _text_body(post)
            body["attachments"] = [
                {"type": item.kind.value, "payload": {"preview": item.label}}
                for item in post.media
            ]
            return PublishResult(
                platform=self.platform,
                chat_id=self._chat_id,
                dry_run=True,
                raw={"request_body": body, "chat_id": self._chat_id},
            )

        body = await self.build_body(post)
        response = await self._send_with_attachment_retry(post, body)
        result = _result_from_response(self._chat_id, response)
        logger.info(
            "max.post.published",
            extra={
                "chat_id": self._chat_id,
                "message_id": result.message_id,
                "attachments": len(post.media),
                "text_length": len(post.text),
            },
        )
        return result

    async def build_body(self, post: Post) -> dict[str, Any]:
        """Message body for POST /messages, uploading media as needed."""
        _validate(post)
        body = _text_body(post)
        attachments = [await self._client.upload(item) for item in post.media]
        if attachments:
            body["attachments"] = attachments
        return body

    async def edit(self, message_id: str, post: Post) -> PublishResult:
        """Replace the content of a post the bot published earlier."""
        _validate(post)
        if self._dry_run:
            return PublishResult(
                platform=self.platform,
                chat_id=self._chat_id,
                message_id=message_id,
                dry_run=True,
                raw={"request_body": _text_body(post)},
            )
        body = await self.build_body(post)
        response = await self._client.edit_message(message_id=message_id, body=body)
        _raise_if_unsuccessful(response, action="edit")
        return PublishResult(
            platform=self.platform,
            chat_id=self._chat_id,
            message_id=message_id,
            raw=response,
        )

    async def delete(self, message_id: str) -> None:
        """Delete a post the bot published earlier."""
        if self._dry_run:
            return
        response = await self._client.delete_message(message_id)
        _raise_if_unsuccessful(response, action="delete")

    async def _send_with_attachment_retry(
        self, post: Post, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Send, retrying while the platform is still processing an upload.

        Documented behaviour: a message sent immediately after a large upload
        can fail because the file is not ready yet, and the fix is to wait and
        try again. Only that specific failure is retried here.
        """
        attempts = self._attachment_attempts if post.media else 1
        for attempt in range(1, attempts + 1):
            try:
                return await self._client.send_message(
                    chat_id=self._chat_id,
                    body=body,
                    disable_link_preview=post.disable_link_preview,
                )
            except MaxApiError as exc:
                if attempt == attempts or not is_attachment_not_ready(exc):
                    raise
                delay = self._attachment_retry_delay * attempt
                logger.warning(
                    "max.post.attachment_not_ready",
                    extra={"attempt": attempt, "sleep": delay},
                )
                await asyncio.sleep(delay)
        raise MaxApiError("send failed after attachment retries")  # pragma: no cover


def _validate(post: Post) -> None:
    if len(post.text) > TEXT_LIMIT:
        raise PostValidationError(
            f"post text is {len(post.text)} characters, the MAX limit is {TEXT_LIMIT}; "
            "split it into several posts yourself instead of letting it be cut silently"
        )


def _text_body(post: Post) -> dict[str, Any]:
    body: dict[str, Any] = {"text": post.text, "notify": post.notify}
    if post.format is not PostFormat.PLAIN:
        body["format"] = post.format.value
    return body


def _raise_if_unsuccessful(response: Any, *, action: str) -> None:
    """PUT/DELETE answer with HTTP 200 and ``success: false`` on failure."""
    if isinstance(response, dict) and response.get("success") is False:
        raise MaxApiError(f"{action} failed: {response.get('message') or 'no reason given'}")


def _result_from_response(chat_id: int, response: Any) -> PublishResult:
    message = response.get("message") if isinstance(response, dict) else None
    if not isinstance(message, dict):
        message = response if isinstance(response, dict) else {}
    body = message.get("body") if isinstance(message.get("body"), dict) else {}

    timestamp = message.get("timestamp")
    published_at = None
    if isinstance(timestamp, int | float):
        published_at = datetime.fromtimestamp(timestamp / 1000, tz=UTC)

    return PublishResult(
        platform=Platform.MAX,
        chat_id=chat_id,
        message_id=str(body.get("mid")) if body.get("mid") else None,
        url=message.get("url"),
        published_at=published_at,
        raw=response if isinstance(response, dict) else None,
    )
