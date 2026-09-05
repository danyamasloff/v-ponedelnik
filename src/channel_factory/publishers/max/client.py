"""Async HTTP client for the MAX Bot API.

Reference: https://dev.max.ru/docs-api (base URL, ``Authorization`` header,
``POST /messages``, ``POST /uploads``, ``GET /updates``). Nothing here is
invented: if the API has no method for something, the caller is told so.

The token is never logged — log records carry the path and status only.
"""

from __future__ import annotations

import asyncio
import mimetypes
import random
import time
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx

from channel_factory.core.logging import get_logger
from channel_factory.publishers.base import MediaItem, MediaKind
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

logger = get_logger(__name__)

# The docs name `platform-api2.max.ru`, but that host serves a certificate
# issued by the Russian Trusted Root CA, which is in neither certifi nor the
# Windows store — every request fails with CERTIFICATE_VERIFY_FAILED until that
# root is installed. `platform-api.max.ru` answers the same API with a globally
# trusted certificate, so it is the default here. Override with
# MAX_API_BASE_URL (and MAX_CA_BUNDLE) to use the documented host.
DEFAULT_BASE_URL = "https://platform-api.max.ru"

#: Text limit of one message/post, per the POST /messages reference.
TEXT_LIMIT = 4000

#: The API allows 2 messages per second into one dialog, chat or channel.
MIN_SEND_INTERVAL = 0.5

RETRYABLE_STATUS = frozenset({429, 500, 502, 503, 504})


def is_attachment_not_ready(error: MaxApiError) -> bool:
    """Whether a failed send is the documented "attachment still processing" case.

    The docs say to wait and retry when a message is sent right after a large
    upload, but they do not name the error code. Matching on wording is
    therefore deliberate and deliberately narrow: only 4xx answers that mention
    an attachment are treated this way.
    """
    if error.status_code is None or not (400 <= error.status_code < 500):
        return False
    haystack = f"{error.code or ''} {error}".lower()
    return "attachment" in haystack and ("not" in haystack or "process" in haystack)


class MaxApiClient:
    """Thin transport layer: auth, retries, throttling, error mapping."""

    def __init__(
        self,
        token: str,
        *,
        base_url: str = DEFAULT_BASE_URL,
        timeout: float = 30.0,
        upload_timeout: float = 300.0,
        max_attempts: int = 4,
        min_send_interval: float = MIN_SEND_INTERVAL,
        backoff_base: float = 1.0,
        backoff_cap: float = 30.0,
        ca_bundle: Path | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not token:
            raise MaxAuthError("MAX bot token is empty")
        # Trust stays on. A bundle only adds a root (e.g. the Минцифры one) for
        # this client, instead of trusting it system-wide.
        verify: str | bool = str(ca_bundle) if ca_bundle else True
        self._max_attempts = max(1, max_attempts)
        self._min_send_interval = min_send_interval
        self._backoff_base = backoff_base
        self._backoff_cap = backoff_cap
        self._send_lock = asyncio.Lock()
        self._last_send = 0.0
        self._api = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            transport=transport,
            verify=verify,
            headers={"Authorization": token, "Accept": "application/json"},
        )
        # Uploads go to a different host (fu./iu./vu.*), so they get their own
        # client: the bot token must not travel to a file-storage domain.
        self._uploads = httpx.AsyncClient(
            timeout=upload_timeout, transport=transport, verify=verify
        )

    async def __aenter__(self) -> MaxApiClient:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._api.aclose()
        await self._uploads.aclose()

    # ------------------------------------------------------------------- bot

    async def get_me(self) -> dict[str, Any]:
        """GET /me — who this token belongs to."""
        return await self._request("GET", "/me")

    # ----------------------------------------------------------------- chats

    async def get_chat(self, chat_id: int) -> dict[str, Any]:
        """GET /chats/{chatId} — channel or group chat details."""
        return await self._request("GET", f"/chats/{chat_id}")

    async def get_my_membership(self, chat_id: int) -> dict[str, Any]:
        """GET /chats/{chatId}/members/me — the bot's own rights in the chat."""
        return await self._request("GET", f"/chats/{chat_id}/members/me")

    async def get_updates(
        self,
        *,
        marker: int | None = None,
        timeout: int = 30,
        limit: int = 100,
        types: list[str] | None = None,
    ) -> dict[str, Any]:
        """GET /updates — long polling. Used here only to discover chat ids."""
        params: dict[str, Any] = {"timeout": timeout, "limit": limit}
        if marker is not None:
            params["marker"] = marker
        if types:
            params["types"] = types
        # The socket must outlive the long poll itself, otherwise every call
        # ends in a client-side timeout instead of an empty result.
        return await self._request("GET", "/updates", params=params, timeout=timeout + 15)

    # -------------------------------------------------------------- messages

    async def send_message(
        self,
        *,
        chat_id: int,
        body: dict[str, Any],
        disable_link_preview: bool = False,
    ) -> dict[str, Any]:
        """POST /messages — publish into a chat or channel."""
        params: dict[str, Any] = {"chat_id": chat_id}
        if disable_link_preview:
            params["disable_link_preview"] = "true"
        return await self._request("POST", "/messages", params=params, json=body, throttle=True)

    async def edit_message(self, *, message_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """PUT /messages — edit a message the bot itself sent."""
        return await self._request(
            "PUT", "/messages", params={"message_id": message_id}, json=body, throttle=True
        )

    async def delete_message(self, message_id: str) -> dict[str, Any]:
        """DELETE /messages — remove a message the bot itself sent."""
        return await self._request("DELETE", "/messages", params={"message_id": message_id})

    # --------------------------------------------------------------- uploads

    async def upload(self, item: MediaItem) -> dict[str, Any]:
        """Turn a media item into an attachment object for the message body.

        Three documented steps: ask for an upload URL, POST the file there,
        then reference the resulting token.
        """
        if item.url is not None:
            return {"type": item.kind.value, "payload": {"url": item.url}}

        path = item.path
        if path is None:  # pragma: no cover - MediaItem guarantees one of the two
            raise MaxUploadError("media item has neither path nor url")
        if not path.is_file():
            raise MaxUploadError(f"file not found: {path}")

        started = await self._request("POST", "/uploads", params={"type": item.kind.value})
        upload_url = started.get("url") if isinstance(started, dict) else None
        if not upload_url:
            raise MaxUploadError("POST /uploads returned no upload url", payload=started)

        uploaded = await self._post_file(str(upload_url), path)
        payload = _attachment_payload(item.kind, started, uploaded, str(upload_url))
        logger.info("max.upload.done", extra={"kind": item.kind.value, "file": path.name})
        return {"type": item.kind.value, "payload": payload}

    async def _post_file(self, upload_url: str, path: Path) -> Any:
        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        data = await asyncio.to_thread(path.read_bytes)
        try:
            response = await self._uploads.post(
                upload_url, files={"data": (path.name, data, content_type)}
            )
        except httpx.HTTPError as exc:
            raise MaxTransportError(f"upload failed: {type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise MaxUploadError(
                f"upload rejected: {response.text[:300]}", status_code=response.status_code
            )
        return _parse_json(response)

    # -------------------------------------------------------------- plumbing

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        throttle: bool = False,
        timeout: float | None = None,
    ) -> Any:
        last_error: MaxApiError | None = None
        for attempt in range(1, self._max_attempts + 1):
            if throttle:
                await self._wait_send_slot()
            try:
                response = await self._api.request(
                    method,
                    path,
                    params=params,
                    json=json,
                    timeout=timeout if timeout is not None else httpx.USE_CLIENT_DEFAULT,
                )
            except httpx.HTTPError as exc:
                last_error = MaxTransportError(f"{method} {path}: {type(exc).__name__}: {exc}")
                if attempt == self._max_attempts:
                    raise last_error from exc
                await self._backoff(attempt)
                continue

            if response.status_code < 400:
                logger.debug(
                    "max.request.ok",
                    extra={"method": method, "path": path, "status": response.status_code},
                )
                return _parse_json(response)

            last_error = _to_error(method, path, response)
            if response.status_code not in RETRYABLE_STATUS or attempt == self._max_attempts:
                raise last_error
            logger.warning(
                "max.request.retry",
                extra={
                    "method": method,
                    "path": path,
                    "status": response.status_code,
                    "attempt": attempt,
                },
            )
            await self._backoff(attempt, retry_after=getattr(last_error, "retry_after", None))

        raise last_error or MaxApiError(f"{method} {path}: exhausted retries")

    async def _wait_send_slot(self) -> None:
        async with self._send_lock:
            wait = self._min_send_interval - (time.monotonic() - self._last_send)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_send = time.monotonic()

    async def _backoff(self, attempt: int, *, retry_after: float | None = None) -> None:
        if retry_after is not None:
            await asyncio.sleep(min(retry_after, self._backoff_cap))
            return
        delay = min(self._backoff_cap, self._backoff_base * 2 ** (attempt - 1))
        # Jitter keeps parallel senders from lining up on the same retry tick.
        await asyncio.sleep(delay + random.uniform(0, delay * 0.25))


def _parse_json(response: httpx.Response) -> Any:
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return {"raw_body": response.text}


def _to_error(method: str, path: str, response: httpx.Response) -> MaxApiError:
    payload = _parse_json(response)
    code = None
    message = response.text[:300]
    if isinstance(payload, dict):
        code = payload.get("code")
        message = str(payload.get("message") or message)

    status = response.status_code
    common: dict[str, Any] = {"status_code": status, "code": code, "payload": payload}
    text = f"{method} {path}: {message}"
    if status == 401:
        return MaxAuthError(text, **common)
    if status == 403:
        return MaxPermissionError(text, **common)
    if status == 404:
        return MaxNotFoundError(text, **common)
    if status == 429:
        return MaxRateLimitError(text, retry_after=_retry_after(response), **common)
    if status >= 500:
        return MaxServerError(text, **common)
    return MaxApiError(text, **common)


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _attachment_payload(
    kind: MediaKind,
    started: Any,
    uploaded: Any,
    upload_url: str,
) -> dict[str, Any]:
    """Build the ``payload`` of an attachment.

    The token surfaces in a different place depending on media type: in the
    /uploads answer for video and audio, in the upload answer for images and
    files, and — per the docs — inside the upload URL itself. All three are
    accepted instead of branching on a type-specific guess.
    """
    if kind is MediaKind.IMAGE and isinstance(uploaded, dict):
        photos = uploaded.get("photos")
        if isinstance(photos, dict) and photos:
            return {"photos": photos}

    for candidate in (uploaded, started):
        if isinstance(candidate, dict) and candidate.get("token"):
            return {"token": str(candidate["token"])}

    from_url = parse_qs(urlsplit(upload_url).query).get("token")
    if from_url and from_url[0]:
        return {"token": from_url[0]}

    raise MaxUploadError(
        "upload produced no attachment token",
        payload={"uploads_response": started, "upload_response": uploaded},
    )
