"""Unit tests for the MAX publishing adapter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from channel_factory.core.enums import Platform
from channel_factory.publishers.base import (
    MediaItem,
    MediaKind,
    Post,
    PostFormat,
    PostValidationError,
)
from channel_factory.publishers.max.adapter import MaxPublisher
from channel_factory.publishers.max.client import TEXT_LIMIT, MaxApiClient
from channel_factory.publishers.max.errors import MaxApiError

CHAT_ID = -1001


def make_publisher(handler: Any, **kwargs: Any) -> tuple[MaxApiClient, MaxPublisher]:
    client = MaxApiClient(
        "test-token",
        transport=httpx.MockTransport(handler),
        backoff_base=0.0,
        min_send_interval=0.0,
    )
    kwargs.setdefault("attachment_retry_delay", 0.0)
    return client, MaxPublisher(client, CHAT_ID, **kwargs)


def refuse(request: httpx.Request) -> httpx.Response:  # pragma: no cover - must not run
    raise AssertionError(f"unexpected HTTP call: {request.method} {request.url}")


class TestPostValidation:
    def test_empty_post_is_rejected(self) -> None:
        with pytest.raises(PostValidationError):
            Post(text="   ")

    def test_media_item_needs_exactly_one_source(self) -> None:
        with pytest.raises(PostValidationError):
            MediaItem(kind=MediaKind.IMAGE, path=Path("a.png"), url="https://example.com/a.png")

    def test_only_images_can_be_attached_by_url(self) -> None:
        with pytest.raises(PostValidationError):
            MediaItem(kind=MediaKind.VIDEO, url="https://example.com/a.mp4")

    @pytest.mark.parametrize(
        ("name", "kind"),
        [
            ("a.png", MediaKind.IMAGE),
            ("a.JPEG", MediaKind.IMAGE),
            ("a.mp4", MediaKind.VIDEO),
            ("a.mp3", MediaKind.AUDIO),
            ("a.pdf", MediaKind.FILE),
            ("a.unknown", MediaKind.FILE),
        ],
    )
    def test_kind_is_inferred_from_the_extension(self, name: str, kind: MediaKind) -> None:
        assert MediaItem.from_path(Path(name)).kind is kind

    async def test_too_long_text_is_refused_before_sending(self) -> None:
        client, publisher = make_publisher(refuse)
        try:
            with pytest.raises(PostValidationError) as exc_info:
                await publisher.publish(Post(text="a" * (TEXT_LIMIT + 1)))
        finally:
            await client.aclose()
        assert str(TEXT_LIMIT) in str(exc_info.value)


class TestBuildBody:
    async def test_plain_post_omits_the_format_field(self) -> None:
        client, publisher = make_publisher(refuse)
        try:
            body = await publisher.build_body(Post(text="привет"))
        finally:
            await client.aclose()
        assert body == {"text": "привет", "notify": True}

    async def test_markdown_and_silent_are_passed_through(self) -> None:
        client, publisher = make_publisher(refuse)
        try:
            body = await publisher.build_body(
                Post(text="*bold*", format=PostFormat.MARKDOWN, notify=False)
            )
        finally:
            await client.aclose()
        assert body == {"text": "*bold*", "notify": False, "format": "markdown"}

    async def test_media_becomes_attachments(self, tmp_path: Path) -> None:
        image = tmp_path / "cover.png"
        image.write_bytes(b"binary")

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/uploads":
                assert request.url.params["type"] == "image"
                return httpx.Response(200, json={"url": "https://iu.example/up.do"})
            return httpx.Response(200, json={"token": "img-token"})

        client, publisher = make_publisher(handler)
        try:
            body = await publisher.build_body(
                Post(text="с картинкой", media=(MediaItem.from_path(image),))
            )
        finally:
            await client.aclose()

        assert body["attachments"] == [{"type": "image", "payload": {"token": "img-token"}}]


class TestPublish:
    async def test_reads_message_id_url_and_timestamp(self) -> None:
        response = {
            "message": {
                "timestamp": 1_757_000_000_000,
                "url": "https://max.ru/post/1",
                "recipient": {"chat_id": CHAT_ID, "chat_type": "channel"},
                "body": {"mid": "mid-42", "seq": 1, "text": "hi"},
            }
        }
        client, publisher = make_publisher(lambda request: httpx.Response(200, json=response))
        try:
            result = await publisher.publish(Post(text="hi"))
        finally:
            await client.aclose()

        assert result.platform is Platform.MAX
        assert result.chat_id == CHAT_ID
        assert result.message_id == "mid-42"
        assert result.url == "https://max.ru/post/1"
        assert result.published_at is not None
        assert result.published_at.year == 2025
        assert result.raw == response

    async def test_result_survives_a_response_without_a_message_id(self) -> None:
        client, publisher = make_publisher(lambda request: httpx.Response(200, json={}))
        try:
            result = await publisher.publish(Post(text="hi"))
        finally:
            await client.aclose()
        assert result.message_id is None
        assert result.raw == {}

    async def test_dry_run_sends_nothing(self, tmp_path: Path) -> None:
        image = tmp_path / "cover.png"
        image.write_bytes(b"binary")
        client, publisher = make_publisher(refuse, dry_run=True)
        try:
            result = await publisher.publish(
                Post(text="черновик", media=(MediaItem.from_path(image),))
            )
        finally:
            await client.aclose()

        assert result.dry_run is True
        assert result.message_id is None
        assert result.raw is not None
        assert result.raw["request_body"]["text"] == "черновик"
        assert result.raw["request_body"]["attachments"][0]["type"] == "image"

    async def test_retries_while_the_attachment_is_still_processing(self, tmp_path: Path) -> None:
        video = tmp_path / "clip.mp4"
        video.write_bytes(b"video")
        sends = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal sends
            if request.url.path == "/uploads":
                return httpx.Response(200, json={"url": "https://vu.example/up.do?token=t"})
            if request.url.host == "vu.example":
                return httpx.Response(200, json={"token": "t"})
            sends += 1
            if sends == 1:
                return httpx.Response(400, json={"code": "attachment.not.ready", "message": "wait"})
            return httpx.Response(200, json={"message": {"body": {"mid": "mid-1"}}})

        client, publisher = make_publisher(handler)
        try:
            result = await publisher.publish(
                Post(text="видео", media=(MediaItem.from_path(video),))
            )
        finally:
            await client.aclose()

        assert sends == 2
        assert result.message_id == "mid-1"

    async def test_other_errors_are_not_retried(self) -> None:
        sends = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal sends
            sends += 1
            return httpx.Response(400, json={"code": "text.too.long", "message": "too long"})

        client, publisher = make_publisher(handler)
        try:
            with pytest.raises(MaxApiError):
                await publisher.publish(Post(text="hi"))
        finally:
            await client.aclose()

        assert sends == 1


class TestEditAndDelete:
    async def test_edit_sends_the_message_id_and_new_body(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"success": True})

        client, publisher = make_publisher(handler)
        try:
            result = await publisher.edit("mid-1", Post(text="новый текст"))
        finally:
            await client.aclose()

        assert seen[0].method == "PUT"
        assert seen[0].url.params["message_id"] == "mid-1"
        assert json.loads(seen[0].content)["text"] == "новый текст"
        assert result.message_id == "mid-1"

    async def test_unsuccessful_edit_raises(self) -> None:
        client, publisher = make_publisher(
            lambda request: httpx.Response(200, json={"success": False, "message": "too old"})
        )
        try:
            with pytest.raises(MaxApiError) as exc_info:
                await publisher.edit("mid-1", Post(text="новый текст"))
        finally:
            await client.aclose()
        assert "too old" in str(exc_info.value)

    async def test_delete_calls_the_api(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"success": True})

        client, publisher = make_publisher(handler)
        try:
            await publisher.delete("mid-1")
        finally:
            await client.aclose()

        assert seen[0].method == "DELETE"
        assert seen[0].url.params["message_id"] == "mid-1"

    async def test_delete_in_dry_run_sends_nothing(self) -> None:
        client, publisher = make_publisher(refuse, dry_run=True)
        try:
            await publisher.delete("mid-1")
        finally:
            await client.aclose()


class TestPreflight:
    @staticmethod
    def _handler(
        *,
        chat: dict[str, Any],
        membership: dict[str, Any] | None,
        membership_status: int = 200,
    ) -> Any:
        def handler(request: httpx.Request) -> httpx.Response:
            path = request.url.path
            if path == "/me":
                return httpx.Response(
                    200, json={"user_id": 1, "first_name": "Бот", "username": "bot", "is_bot": True}
                )
            if path.endswith("/members/me"):
                if membership is None:
                    return httpx.Response(membership_status, json={"message": "forbidden"})
                return httpx.Response(200, json=membership)
            return httpx.Response(200, json=chat)

        return handler

    async def test_admin_of_an_active_channel_can_post(self) -> None:
        handler = self._handler(
            chat={
                "chat_id": CHAT_ID,
                "type": "channel",
                "status": "active",
                "title": "Ниша",
                "participants_count": 12,
            },
            membership={"is_admin": True, "is_owner": False, "permissions": ["write"]},
        )
        client, publisher = make_publisher(handler)
        try:
            preflight = await publisher.preflight()
        finally:
            await client.aclose()

        assert preflight.ok
        assert preflight.can_post is True
        assert preflight.chat.title == "Ниша"
        assert preflight.bot.username == "bot"

    async def test_member_without_rights_cannot_post(self) -> None:
        handler = self._handler(
            chat={"chat_id": CHAT_ID, "type": "channel", "status": "active"},
            membership={"is_admin": False, "is_owner": False, "permissions": []},
        )
        client, publisher = make_publisher(handler)
        try:
            preflight = await publisher.preflight()
        finally:
            await client.aclose()

        assert preflight.can_post is False
        assert not preflight.ok
        assert any("administrator" in problem for problem in preflight.problems)

    async def test_dialog_is_not_a_publishing_target(self) -> None:
        handler = self._handler(
            chat={"chat_id": CHAT_ID, "type": "dialog", "status": "active"},
            membership={"is_admin": True, "permissions": ["write"]},
        )
        client, publisher = make_publisher(handler)
        try:
            preflight = await publisher.preflight()
        finally:
            await client.aclose()

        assert not preflight.ok
        assert any("not a channel" in problem for problem in preflight.problems)

    async def test_unreadable_membership_is_reported_as_unknown(self) -> None:
        handler = self._handler(
            chat={"chat_id": CHAT_ID, "type": "channel", "status": "active"},
            membership=None,
            membership_status=403,
        )
        client, publisher = make_publisher(handler)
        try:
            preflight = await publisher.preflight()
        finally:
            await client.aclose()

        assert preflight.can_post is None
        assert not preflight.ok
        assert any("membership" in problem for problem in preflight.problems)
