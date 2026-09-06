"""MAX publisher: a prepared post in, a platform result out.

Implements the :class:`channel_factory.publishers.base.Publisher` protocol, so
the same kill switch, the same validation and the same stored payload apply to
MAX as to any platform added later. Everything HTTP lives in ``client.py``;
this module owns the publishing rules — what a valid post is, how media becomes
attachments, and whether the channel is one we may post to at all.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from channel_factory.core.enums import Platform
from channel_factory.core.logging import get_logger
from channel_factory.publishers.base import (
    PostFormat,
    PostValidationError,
    PublishError,
    PublishRequest,
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

#: Marker left by the drafting stage where the original analysis must go.
#: Publishing a post that still contains it would ship a skeleton.
PLACEHOLDER_MARKER = "[TODO"


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
    """Publishes posts into MAX channels through the Bot API."""

    platform = Platform.MAX
    text_limit = TEXT_LIMIT

    def __init__(
        self,
        client: MaxApiClient,
        chat_id: int | None = None,
        *,
        attachment_attempts: int = 4,
        attachment_retry_delay: float = 5.0,
    ) -> None:
        self._client = client
        self._chat_id = chat_id
        self._attachment_attempts = max(1, attachment_attempts)
        self._attachment_retry_delay = attachment_retry_delay

    @property
    def chat_id(self) -> int | None:
        return self._chat_id

    def target(self, request: PublishRequest) -> int:
        """Which chat this request goes to: its own, else the configured one."""
        ref = request.channel_ref or (str(self._chat_id) if self._chat_id is not None else "")
        if not ref:
            raise PublishError("no channel id: pass channel_ref or configure MAX_CHAT_ID")
        try:
            return int(ref)
        except ValueError as exc:
            raise PublishError(f"MAX chat_id must be numeric, got {ref!r}") from exc

    # -------------------------------------------------------------- contract

    def validate(self, request: PublishRequest) -> list[str]:
        """Problems worth catching before we spend a request on them."""
        problems: list[str] = []
        if not request.text.strip() and not request.media:
            problems.append("текст пустой и нет вложений")
        if request.length > self.text_limit:
            problems.append(
                f"текст длиннее лимита MAX: {request.length} из {self.text_limit} символов"
            )
        ref = request.channel_ref or (str(self._chat_id) if self._chat_id is not None else "")
        if not ref:
            problems.append("не указан chat_id канала")
        elif not str(ref).lstrip("-").isdigit():
            # The API types chat_id as an integer; a handle would fail server
            # side with a much less obvious message.
            problems.append(f"chat_id должен быть числом, получено {ref!r}")
        if PLACEHOLDER_MARKER in request.text:
            problems.append("в тексте остались незаполненные заготовки")
        for item in request.media:
            if item.path is not None and not item.path.is_file():
                problems.append(f"файл вложения не найден: {item.path}")
        return problems

    def build_payload(self, request: PublishRequest) -> dict[str, Any]:
        """The exact HTTP request that would be sent, minus the media upload.

        Attachments appear as descriptors rather than tokens: obtaining a token
        means actually uploading the file, which a rehearsal must not do. The
        token-free shape is also what makes this payload safe to store and to
        print in dry-run reports.
        """
        body: dict[str, Any] = {"text": request.text, "notify": request.notify}
        if request.format is not PostFormat.PLAIN:
            body["format"] = request.format.value
        if request.media:
            body["attachments"] = [
                {"type": item.kind.value, "payload": {"source": item.label}}
                for item in request.media
            ]

        params: dict[str, Any] = {"chat_id": request.channel_ref or self._chat_id}
        if request.disable_link_preview:
            params["disable_link_preview"] = True

        return {
            "method": "POST",
            "url": "/messages",
            "params": params,
            # The token is never included here: this payload is written to the
            # database and printed in dry-run reports.
            "headers": {"Authorization": "<MAX_BOT_TOKEN>", "Content-Type": "application/json"},
            "body": body,
        }

    async def publish(self, request: PublishRequest) -> PublishResult:
        """Send the post. Only ever called when the kill switch allows it."""
        problems = self.validate(request)
        if problems:
            raise PostValidationError("; ".join(problems))

        chat_id = self.target(request)
        body = await self.build_body(request)
        response = await self._send_with_attachment_retry(request, chat_id, body)
        result = _result_from_response(chat_id, response)
        logger.info(
            "max.post.published",
            extra={
                "chat_id": chat_id,
                "message_id": result.external_message_id,
                "attachments": len(request.media),
                "text_length": request.length,
            },
        )
        return result

    async def build_body(self, request: PublishRequest) -> dict[str, Any]:
        """Message body for POST /messages, uploading media as needed."""
        body: dict[str, Any] = {"text": request.text, "notify": request.notify}
        if request.format is not PostFormat.PLAIN:
            body["format"] = request.format.value
        attachments = [await self._client.upload(item) for item in request.media]
        if attachments:
            body["attachments"] = attachments
        return body

    # ------------------------------------------------------------- preflight

    async def preflight(self, chat_id: int | None = None) -> MaxPreflight:
        """Check token, chat and posting rights before anything is published."""
        target = chat_id if chat_id is not None else self._chat_id
        if target is None:
            raise PublishError("no channel id: configure MAX_CHAT_ID or pass --chat-id")

        bot = MaxBotInfo.from_api(await self._client.get_me())
        chat = MaxChatInfo.from_api(await self._client.get_chat(target))

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
            membership = await self._client.get_my_membership(target)
        except MaxApiError as exc:
            # 403/404 here usually means the bot is not an administrator, but
            # the API does not promise that, so it is reported, not assumed.
            problems.append(f"could not read bot membership: {exc}")
        else:
            is_admin = bool(membership.get("is_admin"))
            is_owner = bool(membership.get("is_owner"))
            raw_permissions = membership.get("permissions") or []
            permissions = tuple(str(item) for item in raw_permissions)
            can_post = bool(is_owner or is_admin or (set(permissions) & POSTING_PERMISSIONS))
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

    # -------------------------------------------------------- edit and delete

    async def edit(self, message_id: str, request: PublishRequest) -> PublishResult:
        """Replace the content of a post the bot published earlier."""
        problems = self.validate(request)
        if problems:
            raise PostValidationError("; ".join(problems))
        body = await self.build_body(request)
        response = await self._client.edit_message(message_id=message_id, body=body)
        _raise_if_unsuccessful(response, action="edit")
        return PublishResult(external_message_id=message_id, raw_response=response)

    async def delete(self, message_id: str) -> None:
        """Delete a post the bot published earlier."""
        response = await self._client.delete_message(message_id)
        _raise_if_unsuccessful(response, action="delete")

    async def _send_with_attachment_retry(
        self, request: PublishRequest, chat_id: int, body: dict[str, Any]
    ) -> dict[str, Any]:
        """Send, retrying while the platform is still processing an upload.

        Documented behaviour: a message sent immediately after a large upload
        can fail because the file is not ready yet, and the fix is to wait and
        try again. Only that specific failure is retried here.
        """
        attempts = self._attachment_attempts if request.media else 1
        for attempt in range(1, attempts + 1):
            try:
                return await self._client.send_message(
                    chat_id=chat_id,
                    body=body,
                    disable_link_preview=request.disable_link_preview,
                )
            except MaxApiError as exc:
                if attempt == attempts or not is_attachment_not_ready(exc):
                    raise
                delay = self._attachment_retry_delay * attempt
                logger.warning(
                    "max.post.attachment_not_ready", extra={"attempt": attempt, "sleep": delay}
                )
                await asyncio.sleep(delay)
        raise MaxApiError("send failed after attachment retries")  # pragma: no cover


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
        external_message_id=str(body.get("mid")) if body.get("mid") else None,
        raw_response=response if isinstance(response, dict) else {},
        url=message.get("url"),
        published_at=published_at,
        channel_ref=str(chat_id),
    )
