"""PHASE 6: rendering the picture that goes with a post.

A card is drawn locally with Pillow — no image API, no stock service, no cost
and no licence question about what ends up in the channel. What it shows is
only what we already know about the topic: headline, vendor and the kind of
event. Nothing is invented on the picture that is not in the post.

Design decisions worth stating:

* 1200x630 — the ratio messengers preview without cropping the headline.
* The headline is wrapped and shrunk to fit; it is never cut mid-word, because
  a truncated headline reads as a bug to the reader.
* Colour comes from the vendor name, deterministically: the same vendor always
  gets the same accent, so a feed of cards looks like a series rather than a
  random assortment.
* Fonts are found on the machine; if none is found we say so instead of
  rendering an unreadable bitmap-font card.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from channel_factory.core.logging import get_logger

logger = get_logger(__name__)

CARD_WIDTH = 1200
CARD_HEIGHT = 630
MARGIN = 72

# Deep, low-saturation backgrounds: the card sits in a chat, not on a billboard.
PALETTE: tuple[tuple[int, int, int], ...] = (
    (23, 42, 69),
    (38, 34, 66),
    (18, 51, 51),
    (54, 33, 44),
    (30, 43, 33),
    (48, 41, 28),
)

TEXT_COLOR = (243, 246, 250)
MUTED_COLOR = (168, 182, 200)

# Fonts shipped with Windows that cover Cyrillic. Checked in order; the first
# that exists wins. A missing font is reported, never silently substituted.
FONT_CANDIDATES: tuple[tuple[str, str], ...] = (
    ("C:/Windows/Fonts/segoeuib.ttf", "C:/Windows/Fonts/segoeui.ttf"),
    ("C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/arial.ttf"),
    (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ),
)

MAX_TITLE_LINES = 5
TITLE_SIZES = (72, 64, 56, 48, 42, 36)


class CardRenderError(RuntimeError):
    """The card could not be drawn."""


@dataclass(frozen=True)
class CardContent:
    """What goes on the card. All of it comes from the post itself."""

    title: str
    vendor: str | None = None
    kicker: str | None = None
    footer: str | None = None


def accent_for(seed: str) -> tuple[int, int, int]:
    """Stable colour for a vendor: same name, same card colour, always."""
    digest = hashlib.sha256(seed.lower().encode("utf-8")).digest()
    return PALETTE[digest[0] % len(PALETTE)]


def _load_fonts() -> tuple[str, str]:
    for bold, regular in FONT_CANDIDATES:
        if Path(bold).is_file() and Path(regular).is_file():
            return bold, regular
    raise CardRenderError(
        "no usable TrueType font found; install one or pass --font "
        f"(looked for: {', '.join(bold for bold, _ in FONT_CANDIDATES)})"
    )


def _wrap(text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    """Greedy word wrap measured with the actual font, not by character count."""
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if font.getlength(candidate) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _fit_title(
    title: str, bold_font_path: str, max_width: int
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Largest size at which the headline fits in the allotted lines."""
    for size in TITLE_SIZES:
        font = ImageFont.truetype(bold_font_path, size)
        lines = _wrap(title, font, max_width)
        if len(lines) <= MAX_TITLE_LINES:
            return font, lines
    # Even at the smallest size it does not fit: keep whole words and mark the
    # cut, rather than slicing through one.
    font = ImageFont.truetype(bold_font_path, TITLE_SIZES[-1])
    lines = _wrap(title, font, max_width)[:MAX_TITLE_LINES]
    lines[-1] = lines[-1] + "…"
    return font, lines


def _clean(text: str) -> str:
    """Collapse whitespace and drop control characters."""
    return " ".join(
        part for part in "".join(ch for ch in text if unicodedata.category(ch)[0] != "C").split()
    )


def render_card(content: CardContent, destination: Path) -> Path:
    """Draw the card and write it as PNG. Returns the path written."""
    title = _clean(content.title)
    if not title:
        raise CardRenderError("card needs a title")

    bold_path, regular_path = _load_fonts()
    background = accent_for(content.vendor or title)

    image = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), background)
    draw = ImageDraw.Draw(image)

    # A brighter bar keyed to the same accent: enough structure to read as a
    # designed card, cheap enough to stay legible at thumbnail size.
    bar_color = tuple(min(255, channel + 90) for channel in background)
    draw.rectangle((0, 0, 12, CARD_HEIGHT), fill=bar_color)

    text_left = MARGIN
    max_width = CARD_WIDTH - MARGIN * 2

    kicker_font = ImageFont.truetype(regular_path, 30)
    footer_font = ImageFont.truetype(regular_path, 28)
    title_font, title_lines = _fit_title(title, bold_path, max_width)

    kicker = _clean(content.kicker or content.vendor or "")
    y = MARGIN
    if kicker:
        draw.text((text_left, y), kicker.upper(), font=kicker_font, fill=MUTED_COLOR)
        y += 60

    line_height = title_font.size + 14
    block_height = line_height * len(title_lines)
    # Vertically centre the headline in the space between kicker and footer.
    available_top = y
    available_bottom = CARD_HEIGHT - MARGIN - 50
    y = max(available_top, available_top + (available_bottom - available_top - block_height) // 2)

    for line in title_lines:
        draw.text((text_left, y), line, font=title_font, fill=TEXT_COLOR)
        y += line_height

    footer = _clean(content.footer or "")
    if footer:
        draw.text(
            (text_left, CARD_HEIGHT - MARGIN - 30), footer, font=footer_font, fill=MUTED_COLOR
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG", optimize=True)
    logger.info("card.rendered", extra={"file": destination.name, "lines": len(title_lines)})
    return destination
