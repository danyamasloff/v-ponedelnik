"""The channel's avatar and wordmark, drawn from the same rules as the cards.

Why generate rather than find a logo: anything downloaded carries a licence
question we would have to answer before every repost, and a generated picture
would be one more thing we cannot explain. A wordmark built from the channel's
own name has neither problem — it is ours, it costs nothing, and it matches the
cards because it uses the same palette and the same type.

The avatar is a square: in MAX and Telegram it is shown small and круглым, so
the mark sits in the middle with room around it. Nothing at the edges.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from channel_factory.content.cards import (
    ACCENT_TEXT,
    GREEN,
    PAPER,
    CardRenderError,
    _clean,
    _load_fonts,
)
from channel_factory.core.logging import get_logger

logger = get_logger(__name__)

AVATAR_SIZE = 640

#: Weekday label on the calendar avatar. The channel is called "В понедельник",
#: so the sheet shows Monday and nothing else needs explaining.
CALENDAR_LABEL = "ПН"
#: Everything must survive being shown as a 40 px circle, so the mark is one
#: or two glyphs and nothing else.
MAX_MARK_CHARS = 2


def initials(name: str) -> str:
    """One or two letters from the channel name.

    "В понедельник" -> "Вп". Two letters read as a mark; three start to read
    as an abbreviation nobody can decode at 40 pixels.
    """
    words = [word for word in _clean(name).split() if word]
    if not words:
        raise CardRenderError("channel name is empty")
    if len(words) == 1:
        return words[0][:MAX_MARK_CHARS].capitalize()
    return (words[0][0] + words[1][0]).capitalize()


class AvatarStyle(StrEnum):
    """Which mark the avatar carries."""

    INITIALS = "initials"
    CALENDAR = "calendar"


def render_avatar_calendar(
    destination: Path,
    *,
    label: str = CALENDAR_LABEL,
    day: str = "1",
) -> Path:
    """A tear-off calendar sheet: green header with the weekday, big numeral.

    The metaphor does the work the initials could not: a reader sees Monday and
    understands the channel before reading a word of the description.
    """
    bold_path, _ = _load_fonts()
    image = Image.new("RGB", (AVATAR_SIZE, AVATAR_SIZE), PAPER)
    draw = ImageDraw.Draw(image)

    # The sheet is generous: at 32 px the avatar is mostly this rectangle, so
    # margins around it are wasted pixels.
    sheet_w, sheet_h = 420, 470
    left = (AVATAR_SIZE - sheet_w) // 2
    top = (AVATAR_SIZE - sheet_h) // 2
    header_h = 132

    draw.rectangle((left, top, left + sheet_w, top + sheet_h), fill=(255, 255, 255))
    draw.rectangle((left, top, left + sheet_w, top + header_h), fill=GREEN)

    label_font = ImageFont.truetype(bold_path, 62)
    _draw_centered(draw, label, label_font, left, left + sheet_w, top + 30, PAPER, spacing=8)

    day_font = ImageFont.truetype(bold_path, 240)
    box = draw.textbbox((0, 0), day, font=day_font)
    draw.text(
        (
            left + (sheet_w - (box[2] - box[0])) / 2 - box[0],
            top + header_h + (sheet_h - header_h - (box[3] - box[1])) / 2 - box[1],
        ),
        day,
        font=day_font,
        fill=(28, 33, 31),
    )

    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG", optimize=True)
    logger.info("identity.avatar", extra={"file": destination.name, "style": "calendar"})
    return destination


def _draw_centered(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    left: int,
    right: int,
    top: int,
    fill: tuple[int, int, int],
    *,
    spacing: int = 0,
) -> None:
    """Horizontally centred text, with optional tracking."""
    width = sum(int(font.getlength(char)) + spacing for char in text) - spacing
    x = left + (right - left - width) / 2
    for char in text:
        draw.text((x, top), char, font=font, fill=fill)
        x += int(font.getlength(char)) + spacing


def render_avatar(
    name: str,
    destination: Path,
    *,
    background: tuple[int, int, int] = GREEN,
    ink: tuple[int, int, int] = PAPER,
) -> Path:
    """Square avatar with the channel's initials and one accent rule."""
    mark = initials(name)
    bold_path, _ = _load_fonts()

    image = Image.new("RGB", (AVATAR_SIZE, AVATAR_SIZE), background)
    draw = ImageDraw.Draw(image)

    font = ImageFont.truetype(bold_path, 300)
    left, top, right, bottom = draw.textbbox((0, 0), mark, font=font)
    draw.text(
        ((AVATAR_SIZE - (right - left)) / 2 - left, (AVATAR_SIZE - (bottom - top)) / 2 - top - 26),
        mark,
        font=font,
        fill=ink,
    )

    # One short rule under the mark: enough to look deliberate at any size.
    rule_width = 120
    draw.rectangle(
        (
            (AVATAR_SIZE - rule_width) // 2,
            AVATAR_SIZE // 2 + 150,
            (AVATAR_SIZE + rule_width) // 2,
            AVATAR_SIZE // 2 + 158,
        ),
        fill=ACCENT_TEXT,
    )

    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG", optimize=True)
    logger.info("identity.avatar", extra={"file": destination.name, "mark": mark})
    return destination


def render_wordmark(
    name: str,
    destination: Path,
    *,
    tagline: str | None = None,
    background: tuple[int, int, int] = (20, 32, 26),
) -> Path:
    """Wide wordmark for a channel header or a cover."""
    label = _clean(name)
    if not label:
        raise CardRenderError("channel name is empty")
    bold_path, regular_path = _load_fonts()

    width, height = 1200, 400
    image = Image.new("RGB", (width, height), background)
    draw = ImageDraw.Draw(image)

    font = ImageFont.truetype(bold_path, 96)
    # Shrink until it fits: a long name must not run off the mark.
    while font.getlength(label) > width - 200 and font.size > 40:
        font = ImageFont.truetype(bold_path, font.size - 4)

    text_width = font.getlength(label)
    x = (width - text_width) / 2
    y = height / 2 - font.size / 2 - (20 if tagline else 0)
    draw.text((x, y), label, font=font, fill=PAPER)

    accent_y = y + font.size + 26
    draw.rectangle((x, accent_y, x + 120, accent_y + 8), fill=ACCENT_TEXT)

    if tagline:
        tagline_font = ImageFont.truetype(regular_path, 30)
        draw.text(
            ((width - tagline_font.getlength(tagline)) / 2, accent_y + 34),
            tagline,
            font=tagline_font,
            fill=(159, 176, 166),
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG", optimize=True)
    logger.info("identity.wordmark", extra={"file": destination.name})
    return destination
