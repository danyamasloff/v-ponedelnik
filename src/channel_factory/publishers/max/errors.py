"""Errors raised by the MAX Bot API client.

The API answers with an HTTP status plus a JSON body that usually carries
``code`` and ``message``. Both are preserved so a CLI can show the platform's
own wording instead of a generic failure.
"""

from __future__ import annotations

from typing import Any

from channel_factory.publishers.base import PublishError


class MaxApiError(PublishError):
    """Any non-success answer from the MAX Bot API."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int | None = None,
        code: str | None = None,
        payload: Any = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.payload = payload

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.status_code is not None:
            parts.append(f"HTTP {self.status_code}")
        if self.code:
            parts.append(f"code={self.code}")
        return " | ".join(parts)


class MaxAuthError(MaxApiError):
    """401: the bot token is missing, wrong or revoked."""


class MaxPermissionError(MaxApiError):
    """403: the bot is not allowed to do this (usually: not a channel admin)."""


class MaxNotFoundError(MaxApiError):
    """404: no such chat, message or upload."""


class MaxRateLimitError(MaxApiError):
    """429: too many requests."""

    def __init__(self, message: str, *, retry_after: float | None = None, **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.retry_after = retry_after


class MaxServerError(MaxApiError):
    """5xx on the platform side."""


class MaxTransportError(MaxApiError):
    """The request never got an answer (DNS, TLS, timeout, connection reset)."""


class MaxUploadError(MaxApiError):
    """The upload flow did not produce an attachment token."""
