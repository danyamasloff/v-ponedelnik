"""Unit tests for chat-id discovery via GET /updates."""

from __future__ import annotations

from typing import Any

import httpx

from channel_factory.publishers.max.client import MaxApiClient
from channel_factory.publishers.max.discovery import discover_chats


def make_client(handler: Any) -> MaxApiClient:
    return MaxApiClient("test-token", transport=httpx.MockTransport(handler), backoff_base=0.0)


class TestDiscoverChats:
    async def test_finds_chat_ids_at_different_nesting_levels(self) -> None:
        payload = {
            "marker": 991,
            "updates": [
                {
                    "update_type": "bot_added",
                    "chat_id": -100,
                    "chat": {"title": "Мой канал"},
                    "user": {"user_id": 5},
                },
                {
                    "update_type": "message_created",
                    "message": {"recipient": {"chat_id": -200, "chat_type": "channel"}},
                },
            ],
        }
        client = make_client(lambda request: httpx.Response(200, json=payload))
        try:
            chats, marker = await discover_chats(client, timeout=0)
        finally:
            await client.aclose()

        assert marker == 991
        assert {chat.chat_id for chat in chats} == {-100, -200}
        by_id = {chat.chat_id: chat for chat in chats}
        assert by_id[-100].title == "Мой канал"
        assert by_id[-200].update_type == "message_created"

    async def test_repeated_chat_is_reported_once(self) -> None:
        payload = {
            "updates": [
                {"update_type": "bot_added", "chat_id": -100},
                {"update_type": "message_created", "message": {"recipient": {"chat_id": -100}}},
            ]
        }
        client = make_client(lambda request: httpx.Response(200, json=payload))
        try:
            chats, marker = await discover_chats(client, timeout=0)
        finally:
            await client.aclose()

        assert [chat.chat_id for chat in chats] == [-100]
        assert marker is None

    async def test_empty_poll_returns_nothing(self) -> None:
        client = make_client(lambda request: httpx.Response(200, json={"updates": []}))
        try:
            chats, _ = await discover_chats(client, timeout=0)
        finally:
            await client.aclose()

        assert chats == []

    async def test_long_poll_timeout_is_passed_to_the_api(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"updates": []})

        client = make_client(handler)
        try:
            await discover_chats(client, timeout=5, marker=17)
        finally:
            await client.aclose()

        assert seen[0].url.params["timeout"] == "5"
        assert seen[0].url.params["marker"] == "17"
        assert "bot_added" in seen[0].url.params.get_list("types")
