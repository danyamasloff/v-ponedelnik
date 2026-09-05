"""Unit tests for the MAX Bot API client.

Everything runs against httpx.MockTransport: no network, no token, no bot.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from channel_factory.publishers.base import MediaItem, MediaKind
from channel_factory.publishers.max.client import (
    MaxApiClient,
    is_attachment_not_ready,
)
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

TOKEN = "test-token"


def make_client(handler: Any, **kwargs: Any) -> MaxApiClient:
    """Client with retries that do not sleep, so tests stay fast."""
    kwargs.setdefault("backoff_base", 0.0)
    kwargs.setdefault("min_send_interval", 0.0)
    return MaxApiClient(TOKEN, transport=httpx.MockTransport(handler), **kwargs)


class TestRequests:
    async def test_sends_token_in_authorization_header(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"user_id": 1, "is_bot": True})

        async with make_client(handler) as client:
            await client.get_me()

        assert seen[0].headers["Authorization"] == TOKEN
        assert seen[0].url.path == "/me"

    async def test_send_message_passes_chat_id_as_query(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"message": {"body": {"mid": "mid-1"}}})

        async with make_client(handler) as client:
            await client.send_message(chat_id=-42, body={"text": "hi"}, disable_link_preview=True)

        request = seen[0]
        assert request.url.params["chat_id"] == "-42"
        assert request.url.params["disable_link_preview"] == "true"
        assert json.loads(request.content) == {"text": "hi"}

    async def test_empty_body_is_not_a_parse_error(self) -> None:
        async with make_client(lambda request: httpx.Response(200)) as client:
            assert await client.delete_message("mid-1") == {}


class TestErrorMapping:
    @pytest.mark.parametrize(
        ("status", "expected"),
        [
            (401, MaxAuthError),
            (403, MaxPermissionError),
            (404, MaxNotFoundError),
            (400, MaxApiError),
        ],
    )
    async def test_status_maps_to_error_type(self, status: int, expected: type) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(status, json={"code": "some.code", "message": "нет доступа"})

        async with make_client(handler) as client:
            with pytest.raises(expected) as exc_info:
                await client.get_chat(1)

        error = exc_info.value
        assert error.status_code == status
        assert error.code == "some.code"
        assert "нет доступа" in str(error)

    async def test_client_error_is_not_retried(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(400, json={"message": "bad"})

        async with make_client(handler) as client:
            with pytest.raises(MaxApiError):
                await client.get_chat(1)

        assert calls == 1

    async def test_server_error_is_retried_then_succeeds(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            if calls < 3:
                return httpx.Response(503, json={"message": "unavailable"})
            return httpx.Response(200, json={"user_id": 7})

        async with make_client(handler) as client:
            assert await client.get_me() == {"user_id": 7}

        assert calls == 3

    async def test_rate_limit_gives_up_after_max_attempts(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"message": "slow down"})

        async with make_client(handler, max_attempts=2) as client:
            with pytest.raises(MaxRateLimitError) as exc_info:
                await client.get_me()

        assert calls == 2
        assert exc_info.value.retry_after == 0

    async def test_server_error_after_all_attempts_is_raised(self) -> None:
        async with make_client(lambda request: httpx.Response(500), max_attempts=2) as client:
            with pytest.raises(MaxServerError):
                await client.get_me()

    async def test_transport_failure_becomes_transport_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route", request=request)

        async with make_client(handler, max_attempts=2) as client:
            with pytest.raises(MaxTransportError):
                await client.get_me()

    async def test_empty_token_is_rejected_before_any_request(self) -> None:
        with pytest.raises(MaxAuthError):
            MaxApiClient("")


class TestUploads:
    def _upload_handler(self, upload_response: Any, *, uploads_body: dict | None = None) -> Any:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/uploads":
                return httpx.Response(200, json=uploads_body or {"url": "https://iu.example/up.do"})
            return httpx.Response(200, json=upload_response)

        return handler

    async def test_image_upload_returns_photos_payload(self, tmp_path: Any) -> None:
        path = tmp_path / "cover.png"
        path.write_bytes(b"binary")
        photos = {"photo-key": {"token": "photo-token"}}

        async with make_client(self._upload_handler({"photos": photos})) as client:
            attachment = await client.upload(MediaItem.from_path(path))

        assert attachment == {"type": "image", "payload": {"photos": photos}}

    async def test_file_upload_returns_token_payload(self, tmp_path: Any) -> None:
        path = tmp_path / "report.pdf"
        path.write_bytes(b"%PDF-")

        async with make_client(self._upload_handler({"token": "file-token"})) as client:
            attachment = await client.upload(MediaItem.from_path(path))

        assert attachment == {"type": "file", "payload": {"token": "file-token"}}

    async def test_token_from_uploads_response_is_used_when_upload_returns_nothing(
        self, tmp_path: Any
    ) -> None:
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"video")
        handler = self._upload_handler(
            {}, uploads_body={"url": "https://vu.example/up.do", "token": "video-token"}
        )

        async with make_client(handler) as client:
            attachment = await client.upload(MediaItem.from_path(path))

        assert attachment == {"type": "video", "payload": {"token": "video-token"}}

    async def test_token_is_taken_from_the_upload_url_as_last_resort(self, tmp_path: Any) -> None:
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"video")
        handler = self._upload_handler({}, uploads_body={"url": "https://vu.example/up?token=abc"})

        async with make_client(handler) as client:
            attachment = await client.upload(MediaItem.from_path(path))

        assert attachment == {"type": "video", "payload": {"token": "abc"}}

    async def test_missing_token_is_an_upload_error(self, tmp_path: Any) -> None:
        path = tmp_path / "clip.mp4"
        path.write_bytes(b"video")

        async with make_client(self._upload_handler({"status": "ok"})) as client:
            with pytest.raises(MaxUploadError):
                await client.upload(MediaItem.from_path(path))

    async def test_bot_token_is_not_sent_to_the_upload_host(self, tmp_path: Any) -> None:
        path = tmp_path / "cover.png"
        path.write_bytes(b"binary")
        upload_requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/uploads":
                return httpx.Response(200, json={"url": "https://iu.example/up.do"})
            upload_requests.append(request)
            return httpx.Response(200, json={"token": "t"})

        async with make_client(handler) as client:
            await client.upload(MediaItem.from_path(path))

        assert "authorization" not in {k.lower() for k in upload_requests[0].headers}

    async def test_image_url_needs_no_upload(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
            raise AssertionError("no HTTP call expected for a URL attachment")

        async with make_client(handler) as client:
            attachment = await client.upload(MediaItem.from_url("https://example.com/a.png"))

        assert attachment == {
            "type": "image",
            "payload": {"url": "https://example.com/a.png"},
        }

    async def test_missing_file_fails_before_any_request(self, tmp_path: Any) -> None:
        def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
            raise AssertionError("no HTTP call expected for a missing file")

        async with make_client(handler) as client:
            with pytest.raises(MaxUploadError):
                await client.upload(MediaItem(kind=MediaKind.FILE, path=tmp_path / "nope.txt"))


class TestAttachmentNotReady:
    @pytest.mark.parametrize(
        ("status", "code", "message", "expected"),
        [
            (400, "attachment.not.ready", "wait", True),
            (400, None, "Attachment is still processing", True),
            (400, None, "text is too long", False),
            (500, "attachment.not.ready", "wait", False),
        ],
    )
    def test_detection(self, status: int, code: str | None, message: str, expected: bool) -> None:
        error = MaxApiError(message, status_code=status, code=code)
        assert is_attachment_not_ready(error) is expected
