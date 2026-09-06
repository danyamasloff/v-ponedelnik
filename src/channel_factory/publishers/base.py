"""Publisher contract.

A publisher knows one platform's API and nothing else: it does not decide
whether something *should* be published, does not render text, and does not
own the kill switch. That separation is what lets the same post be rehearsed
and then sent through identical code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from channel_factory.core.enums import Platform


class PublishError(Exception):
    """A platform refused or failed the request."""


@dataclass(frozen=True)
class PublishRequest:
    """One post, ready for a platform."""

    text: str
    channel_ref: str
    attachments: list[dict[str, Any]] = field(default_factory=list)
    disable_link_preview: bool = False
    notify: bool = True

    @property
    def length(self) -> int:
        return len(self.text)


@dataclass(frozen=True)
class PublishResult:
    """What the platform said."""

    external_message_id: str | None
    raw_response: dict[str, Any] = field(default_factory=dict)


class Publisher(Protocol):
    """Sends a prepared post to one platform."""

    platform: Platform
    text_limit: int

    def validate(self, request: PublishRequest) -> list[str]:
        """Return problems that would make this post fail or embarrass us."""
        ...

    def build_payload(self, request: PublishRequest) -> dict[str, Any]:
        """The exact request that would be sent — the unit a dry run reviews."""
        ...

    async def publish(self, request: PublishRequest) -> PublishResult:
        """Actually send the post."""
        ...
