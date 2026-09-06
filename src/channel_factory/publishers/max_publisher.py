"""Publishing to our own MAX channel through the official Bot API.

Every detail here is taken from dev.max.ru rather than recalled, because this
API has already changed in ways that would have broken guesses:

* base URL ``https://platform-api2.max.ru``;
* the token goes in the ``Authorization`` header — passing it as a query
  parameter is no longer supported;
* ``chat_id`` is a **query parameter** on ``POST /messages``, not a body field;
* the body carries ``text`` (max 4000 characters), ``attachments``, ``format``
  and ``notify``;
* media is uploaded first via ``POST /uploads``, and the returned token is then
  referenced from an attachment;
* ``GET /chats`` is deprecated, so a channel id is learned from an update when
  the bot is added, not by listing chats;
* the platform allows 30 requests per second.

The bot only ever posts to a channel where it is an administrator. Reading
other people's channels is not possible through this API and is not attempted.
"""

from __future__ import annotations

from typing import Any

import httpx

from channel_factory.core.enums import Platform
from channel_factory.core.logging import get_logger
from channel_factory.publishers.base import PublishError, PublishRequest, PublishResult

logger = get_logger(__name__)

API_ROOT = "https://platform-api2.max.ru"
TEXT_LIMIT = 4000
REQUEST_TIMEOUT_SECONDS = 30.0


class MaxPublisher:
    """Posts to one MAX channel."""

    platform = Platform.MAX
    text_limit = TEXT_LIMIT

    def __init__(self, token: str | None, *, api_root: str = API_ROOT) -> None:
        self._token = token
        self._api_root = api_root.rstrip("/")

    # ------------------------------------------------------------- preparation

    def validate(self, request: PublishRequest) -> list[str]:
        """Problems worth catching before we spend a request on them."""
        problems: list[str] = []
        if not request.text.strip():
            problems.append("текст пустой")
        if request.length > self.text_limit:
            problems.append(
                f"текст длиннее лимита MAX: {request.length} из {self.text_limit} символов"
            )
        if not request.channel_ref:
            problems.append("не указан chat_id канала")
        elif not str(request.channel_ref).lstrip("-").isdigit():
            # The API types chat_id as an integer; a handle would fail server
            # side with a much less obvious message.
            problems.append(f"chat_id должен быть числом, получено {request.channel_ref!r}")
        if "[TODO" in request.text:
            problems.append("в тексте остались незаполненные заготовки")
        return problems

    def build_payload(self, request: PublishRequest) -> dict[str, Any]:
        """The exact HTTP request that would be sent."""
        body: dict[str, Any] = {
            "text": request.text,
            "notify": request.notify,
            "format": "markdown",
        }
        if request.attachments:
            body["attachments"] = request.attachments

        params: dict[str, Any] = {"chat_id": request.channel_ref}
        if request.disable_link_preview:
            params["disable_link_preview"] = True

        return {
            "method": "POST",
            "url": f"{self._api_root}/messages",
            "params": params,
            # The token is never included here: this payload is written to the
            # database and printed in dry-run reports.
            "headers": {"Authorization": "<MAX_BOT_TOKEN>", "Content-Type": "application/json"},
            "body": body,
        }

    # ---------------------------------------------------------------- sending

    def _headers(self) -> dict[str, str]:
        if not self._token:
            raise PublishError(
                "MAX_BOT_TOKEN не задан. Создайте бота в кабинете MAX для партнёров, "
                "добавьте его администратором канала и положите токен в .env."
            )
        return {"Authorization": self._token, "Content-Type": "application/json"}

    async def publish(self, request: PublishRequest) -> PublishResult:
        """Send the post. Only ever called when the kill switch allows it."""
        problems = self.validate(request)
        if problems:
            raise PublishError("; ".join(problems))

        payload = self.build_payload(request)
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                response = await client.post(
                    payload["url"],
                    params=payload["params"],
                    json=payload["body"],
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise PublishError(f"MAX request failed: {type(exc).__name__}: {exc}") from exc

        if response.status_code >= 400:
            raise PublishError(f"MAX returned HTTP {response.status_code}: {response.text[:300]}")

        data = response.json()
        message = data.get("message") or {}
        logger.info(
            "published to MAX",
            extra={"chat_id": request.channel_ref, "message_id": message.get("id")},
        )
        return PublishResult(
            external_message_id=str(message.get("id")) if message.get("id") else None,
            raw_response=data,
        )

    # -------------------------------------------------------------- discovery

    async def fetch_updates(self, *, limit: int = 20) -> list[dict[str, Any]]:
        """Read pending updates.

        This is how a channel id is obtained: ``GET /chats`` is deprecated, so
        the id arrives in the update produced when the bot is added to the
        channel.
        """
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                response = await client.get(
                    f"{self._api_root}/updates",
                    params={"limit": limit},
                    headers=self._headers(),
                )
        except httpx.HTTPError as exc:
            raise PublishError(f"MAX request failed: {type(exc).__name__}: {exc}") from exc

        if response.status_code >= 400:
            raise PublishError(f"MAX returned HTTP {response.status_code}: {response.text[:300]}")
        return list(response.json().get("updates") or [])

    async def whoami(self) -> dict[str, Any]:
        """Bot identity — the cheapest way to prove a token works."""
        try:
            async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                response = await client.get(f"{self._api_root}/me", headers=self._headers())
        except httpx.HTTPError as exc:
            raise PublishError(f"MAX request failed: {type(exc).__name__}: {exc}") from exc

        if response.status_code >= 400:
            raise PublishError(f"MAX returned HTTP {response.status_code}: {response.text[:300]}")
        return dict(response.json())
