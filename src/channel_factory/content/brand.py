"""The channel's voice and signature, in one place.

Both a human and a model write posts here, and the reader should not be able to
tell which. That only works if the rules of tone live in one file that both
paths read — not in a prompt string somewhere and a card template somewhere
else.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

from channel_factory.core.logging import get_logger

logger = get_logger(__name__)


@dataclass(frozen=True)
class Brand:
    """What the channel is called and how it sounds."""

    name: str
    tagline: str = ""
    promise: str = ""
    voice: tuple[str, ...] = ()
    never: tuple[str, ...] = ()
    card_footer: str = ""
    default_kicker: str = "разбор"

    def voice_prompt(self) -> str:
        """The tone rules, formatted for a system prompt.

        Kept as plain lines rather than a paragraph: a model follows a short
        list far more reliably than prose about "our tone of voice".
        """
        lines: list[str] = []
        if self.promise:
            lines.append(f"Обещание канала: {self.promise}.")
        if self.voice:
            lines.append("Как писать:")
            lines += [f"* {rule};" for rule in self.voice]
        if self.never:
            lines.append("Чего в канале не бывает:")
            lines += [f"* {rule};" for rule in self.never]
        return "\n".join(lines)


DEFAULT = Brand(name="Канал", card_footer="")


@lru_cache(maxsize=4)
def load_brand(path: Path) -> Brand:
    """Read the brand file, falling back to a neutral default.

    A missing brand file must not stop publishing: the channel then simply has
    no signature, which is visible and fixable, unlike a crash at 09:00.
    """
    if not path.exists():
        logger.warning("brand.missing", extra={"path": str(path)})
        return DEFAULT
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    card = raw.get("card") or {}
    return Brand(
        name=str(raw.get("name") or DEFAULT.name),
        tagline=str(raw.get("tagline") or ""),
        promise=str(raw.get("promise") or ""),
        voice=tuple(str(item) for item in raw.get("voice") or ()),
        never=tuple(str(item) for item in raw.get("never") or ()),
        card_footer=str(card.get("footer") or ""),
        default_kicker=str(card.get("default_kicker") or DEFAULT.default_kicker),
    )
