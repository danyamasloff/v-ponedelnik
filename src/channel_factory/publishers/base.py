"""Platform-agnostic publishing contract.

Only what every messenger shares lives here: what a post is made of and what
publishing it returns. Everything MAX-specific (tokens, attachment payloads,
rate limits) stays in :mod:`channel_factory.publishers.max`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from channel_factory.core.enums import Platform


class PublishError(RuntimeError):
    """Base class for every publishing failure."""


class PostValidationError(PublishError):
    """The post cannot be sent as written (too long, empty, unsupported media)."""


class PostFormat(StrEnum):
    """How the platform should interpret the post text."""

    PLAIN = "plain"
    MARKDOWN = "markdown"
    HTML = "html"


class MediaKind(StrEnum):
    """Kind of attached media.

    Values match the ``type`` accepted by the MAX uploads endpoint; other
    platforms map them to their own vocabulary.
    """

    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    FILE = "file"


# Extension -> kind. Anything unlisted is uploaded as a generic file rather
# than guessed: a wrong kind makes the platform reject the upload outright.
_KIND_BY_SUFFIX: dict[str, MediaKind] = {
    ".jpg": MediaKind.IMAGE,
    ".jpeg": MediaKind.IMAGE,
    ".png": MediaKind.IMAGE,
    ".gif": MediaKind.IMAGE,
    ".bmp": MediaKind.IMAGE,
    ".tif": MediaKind.IMAGE,
    ".tiff": MediaKind.IMAGE,
    ".heic": MediaKind.IMAGE,
    ".mp4": MediaKind.VIDEO,
    ".mov": MediaKind.VIDEO,
    ".mkv": MediaKind.VIDEO,
    ".webm": MediaKind.VIDEO,
    ".mp3": MediaKind.AUDIO,
    ".wav": MediaKind.AUDIO,
    ".m4a": MediaKind.AUDIO,
}


@dataclass(frozen=True)
class MediaItem:
    """One attachment: either a local file to upload or a remote image URL."""

    kind: MediaKind
    path: Path | None = None
    url: str | None = None

    def __post_init__(self) -> None:
        if (self.path is None) == (self.url is None):
            raise PostValidationError("MediaItem needs exactly one of path or url")
        if self.url is not None and self.kind is not MediaKind.IMAGE:
            # MAX accepts an external URL only for images; everything else has
            # to be uploaded first (docs: POST /uploads).
            raise PostValidationError("only images can be attached by URL")

    @classmethod
    def from_path(cls, path: Path) -> MediaItem:
        kind = _KIND_BY_SUFFIX.get(path.suffix.lower(), MediaKind.FILE)
        return cls(kind=kind, path=path)

    @classmethod
    def from_url(cls, url: str) -> MediaItem:
        return cls(kind=MediaKind.IMAGE, url=url)

    @property
    def label(self) -> str:
        return str(self.path) if self.path is not None else str(self.url)


@dataclass(frozen=True)
class Post:
    """What we want published, before any platform encoding."""

    text: str = ""
    media: tuple[MediaItem, ...] = ()
    format: PostFormat = PostFormat.PLAIN
    notify: bool = True
    disable_link_preview: bool = False

    def __post_init__(self) -> None:
        if not self.text.strip() and not self.media:
            raise PostValidationError("post has neither text nor media")


@dataclass(frozen=True)
class PublishResult:
    """Outcome of one publish attempt.

    ``raw`` keeps the platform response as received: the same rule as for
    market data — never throw the source payload away.
    """

    platform: Platform
    chat_id: int
    message_id: str | None = None
    url: str | None = None
    published_at: datetime | None = None
    dry_run: bool = False
    raw: dict[str, Any] | None = None


class Publisher(Protocol):
    """What every platform adapter must provide."""

    platform: Platform

    async def publish(self, post: Post) -> PublishResult: ...
