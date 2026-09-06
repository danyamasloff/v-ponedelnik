"""Publisher contract.

A publisher knows one platform's API and nothing else: it does not decide
whether something *should* be published, does not render text, and does not
own the kill switch. That separation is what lets the same post be rehearsed
and then sent through identical code.

One request type describes a post for every platform, including its media:
attachments are named as local files or image URLs here, and each publisher
turns them into whatever its own API wants.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from channel_factory.core.enums import Platform


class PublishError(Exception):
    """A platform refused or failed the request."""


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
class PublishRequest:
    """One post, ready for a platform."""

    text: str
    channel_ref: str = ""
    media: tuple[MediaItem, ...] = ()
    format: PostFormat = PostFormat.PLAIN
    disable_link_preview: bool = False
    notify: bool = True

    def __post_init__(self) -> None:
        if not self.text.strip() and not self.media:
            raise PostValidationError("post has neither text nor media")

    @property
    def length(self) -> int:
        return len(self.text)


@dataclass(frozen=True)
class PublishResult:
    """What the platform said.

    ``raw_response`` keeps the answer as received: the same rule as for market
    data — never discard the payload the source gave us.
    """

    external_message_id: str | None = None
    raw_response: dict[str, Any] = field(default_factory=dict)
    url: str | None = None
    published_at: datetime | None = None
    channel_ref: str | None = None
    dry_run: bool = False


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
