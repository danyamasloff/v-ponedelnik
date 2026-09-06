"""PHASE 6: rendering the picture that goes with a post.

A card is drawn locally with Pillow — no image API, no stock service, no cost
and no licence question about what ends up in the channel. What it shows is
only what we already know about the topic: headline, rubric, date, brand.
Nothing is invented on the picture that is not in the post.

**Five styles, one per post.** A channel whose cards are all identical reads
as a template; five layouts in rotation read as a designed series. The style
is chosen deterministically from a seed (the topic id), so re-rendering a post
never changes its card, while consecutive posts almost always differ.

Design decisions worth stating:

* 1200x630 — the ratio messengers preview without cropping the headline.
* The headline is wrapped and shrunk to fit; it is never cut mid-word, because
  a truncated headline reads as a bug to the reader.
* Accent colour comes from the vendor name, deterministically: the same vendor
  always gets the same colour, so the palette stays coherent across styles.
* Fonts are found on the machine; if none is found we say so instead of
  rendering an unreadable bitmap-font card.
"""

from __future__ import annotations

import hashlib
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from enum import StrEnum
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
# The kicker is the only place with a warm tint: it marks the rubric without
# pulling attention from the headline.
ACCENT_TEXT = (233, 196, 106)

# Light style: warm paper rather than pure white — pure white glares in a dark
# chat, and a tinted ground reads as printed.
PAPER = (247, 245, 239)
INK = (28, 33, 31)
PAPER_MUTED = (107, 105, 97)
PAPER_RULE = (226, 221, 208)
GREEN = (47, 111, 79)

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

MONTHS_GENITIVE = (
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)


class CardStyle(StrEnum):
    """The five layouts, named after the artboards they came from."""

    DARK = "dark"
    ACCENT_BLOCK = "accent_block"
    LIGHT = "light"
    BAND = "band"
    MINIMAL = "minimal"


class CardRenderError(RuntimeError):
    """The card could not be drawn."""


@dataclass(frozen=True)
class CardContent:
    """What goes on the card. All of it comes from the post itself."""

    title: str
    vendor: str | None = None
    kicker: str | None = None
    footer: str | None = None
    brand: str | None = None
    published_on: date | None = None


def accent_for(seed: str) -> tuple[int, int, int]:
    """Stable colour for a vendor: same name, same card colour, always."""
    digest = hashlib.sha256(seed.lower().encode("utf-8")).digest()
    return PALETTE[digest[0] % len(PALETTE)]


def style_for(seed: str) -> CardStyle:
    """Stable style for a post.

    Deterministic rather than random, so a re-render never changes a published
    card; keyed on the post rather than the vendor, so consecutive posts almost
    always look different.
    """
    styles = list(CardStyle)
    digest = hashlib.sha256(f"style:{seed}".encode()).digest()
    return styles[digest[0] % len(styles)]


def _lighten(color: tuple[int, int, int], amount: int) -> tuple[int, int, int]:
    """Same hue, more light. Used for rules and the accent bar."""
    return tuple(min(255, channel + amount) for channel in color)  # type: ignore[return-value]


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
    title: str,
    bold_font_path: str,
    max_width: int,
    *,
    sizes: tuple[int, ...] = TITLE_SIZES,
    max_lines: int = MAX_TITLE_LINES,
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Largest size at which the headline fits in the allotted lines."""
    for size in sizes:
        font = ImageFont.truetype(bold_font_path, size)
        lines = _wrap(title, font, max_width)
        if len(lines) <= max_lines:
            return font, lines
    # Even at the smallest size it does not fit: keep whole words and mark the
    # cut, rather than slicing through one.
    font = ImageFont.truetype(bold_font_path, sizes[-1])
    lines = _wrap(title, font, max_width)[:max_lines]
    lines[-1] = lines[-1] + "…"
    return font, lines


def _clean(text: str) -> str:
    """Collapse whitespace and drop control characters."""
    return " ".join(
        part for part in "".join(ch for ch in text if unicodedata.category(ch)[0] != "C").split()
    )


def _draw_tracked(
    draw: ImageDraw.ImageDraw,
    position: tuple[int, int],
    text: str,
    font: ImageFont.FreeTypeFont,
    fill: tuple[int, int, int],
    *,
    spacing: int,
) -> None:
    """Letter-spaced text. Pillow has no tracking, so glyphs are placed by hand."""
    x, y = position
    for char in text:
        draw.text((x, y), char, font=font, fill=fill)
        x += int(font.getlength(char)) + spacing


def _gradient(base: tuple[int, int, int]) -> Image.Image:
    """Vertical gradient from a lighter top to the base colour.

    A flat rectangle reads as a placeholder; a gradient reads as designed, and
    costs one pass over the height of the image.
    """
    top = _lighten(base, 26)
    image = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), base)
    draw = ImageDraw.Draw(image)
    for y in range(CARD_HEIGHT):
        ratio = y / CARD_HEIGHT
        row = tuple(
            int(top[channel] + (base[channel] - top[channel]) * ratio) for channel in range(3)
        )
        draw.line((0, y, CARD_WIDTH, y), fill=row)
    return image


def _draw_arc(image: Image.Image, base: tuple[int, int, int]) -> None:
    """One oversized, barely-there arc in the corner.

    Drawn on its own translucent layer so it lifts the background without
    competing with the headline; at thumbnail size it registers as texture.
    """
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).ellipse(
        (CARD_WIDTH - 260, -240, CARD_WIDTH + 300, 320),
        outline=(*_lighten(base, 120), 90),
        width=3,
    )
    ImageDraw.Draw(layer).ellipse(
        (CARD_WIDTH - 160, -160, CARD_WIDTH + 420, 420),
        outline=(*_lighten(base, 90), 60),
        width=2,
    )
    image.paste(Image.alpha_composite(image.convert("RGBA"), layer).convert("RGB"), (0, 0))


# --------------------------------------------------------------------- styles


def _render_dark(content: CardContent, title: str, fonts: tuple[str, str]) -> Image.Image:
    """Dark gradient, letter-spaced rubric, rule above the signature."""
    bold_path, regular_path = fonts
    background = accent_for(content.vendor or title)

    image = _gradient(background)
    _draw_arc(image, background)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 14, CARD_HEIGHT), fill=_lighten(background, 110))

    max_width = CARD_WIDTH - MARGIN * 2 - 40
    title_font, title_lines = _fit_title(title, bold_path, max_width)

    y = MARGIN
    kicker = _clean(content.kicker or content.vendor or "")
    if kicker:
        _draw_tracked(
            draw,
            (MARGIN, y),
            kicker.upper(),
            ImageFont.truetype(bold_path, 26),
            ACCENT_TEXT,
            spacing=3,
        )
        y += 58

    footer = _clean(content.footer or "")
    footer_top = CARD_HEIGHT - MARGIN - (58 if footer else 0)
    line_height = title_font.size + 16
    y = max(y, y + (footer_top - y - line_height * len(title_lines)) // 2)
    for line in title_lines:
        draw.text((MARGIN, y), line, font=title_font, fill=TEXT_COLOR)
        y += line_height

    if footer:
        rule_y = footer_top - 2
        draw.line((MARGIN, rule_y, MARGIN + 120, rule_y), fill=_lighten(background, 70), width=3)
        draw.text(
            (MARGIN, rule_y + 18),
            footer,
            font=ImageFont.truetype(regular_path, 26),
            fill=MUTED_COLOR,
        )
    return image


def _render_accent_block(content: CardContent, title: str, fonts: tuple[str, str]) -> Image.Image:
    """Date as a large numeral in a colour block, headline beside it."""
    bold_path, regular_path = fonts
    block_width = 300
    image = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), (22, 33, 28))
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, block_width, CARD_HEIGHT), fill=ACCENT_TEXT)

    day = content.published_on or date.today()
    rubric = _clean((content.kicker or "разбор").split("·")[0])

    draw.text((40, 56), rubric.upper(), font=ImageFont.truetype(bold_path, 30), fill=(26, 38, 32))
    draw.text(
        (36, CARD_HEIGHT - 246),
        f"{day.day:02d}",
        font=ImageFont.truetype(bold_path, 132),
        fill=(26, 38, 32),
    )
    draw.text(
        (40, CARD_HEIGHT - 96),
        MONTHS_GENITIVE[day.month - 1],
        font=ImageFont.truetype(regular_path, 30),
        fill=(107, 90, 36),
    )

    text_left = block_width + 56
    max_width = CARD_WIDTH - text_left - 56
    title_font, title_lines = _fit_title(
        title, bold_path, max_width, sizes=(60, 54, 48, 42, 36), max_lines=6
    )
    y = 64
    for line in title_lines:
        draw.text((text_left, y), line, font=title_font, fill=TEXT_COLOR)
        y += title_font.size + 14

    footer = _clean(content.footer or "")
    if footer:
        draw.text(
            (text_left, CARD_HEIGHT - MARGIN - 20),
            footer,
            font=ImageFont.truetype(regular_path, 25),
            fill=(159, 176, 166),
        )
    return image


def _render_light(content: CardContent, title: str, fonts: tuple[str, str]) -> Image.Image:
    """Warm paper, dark ink — stands out in a feed where everything is dark."""
    bold_path, regular_path = fonts
    image = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), PAPER)
    draw = ImageDraw.Draw(image)
    draw.ellipse(
        (CARD_WIDTH - MARGIN - 88, MARGIN, CARD_WIDTH - MARGIN, MARGIN + 88),
        outline=(221, 215, 200),
        width=3,
    )

    y = MARGIN
    kicker = _clean(content.kicker or "")
    if kicker:
        draw.rectangle((MARGIN, y + 12, MARGIN + 42, y + 16), fill=GREEN)
        _draw_tracked(
            draw,
            (MARGIN + 58, y),
            kicker.upper(),
            ImageFont.truetype(bold_path, 24),
            GREEN,
            spacing=3,
        )
        y += 56

    max_width = CARD_WIDTH - MARGIN * 2 - 40
    title_font, title_lines = _fit_title(title, bold_path, max_width, sizes=(70, 62, 54, 46, 40))
    footer_top = CARD_HEIGHT - MARGIN - 60
    y = max(y, y + (footer_top - y - (title_font.size + 16) * len(title_lines)) // 2)
    for line in title_lines:
        draw.text((MARGIN, y), line, font=title_font, fill=INK)
        y += title_font.size + 16

    draw.line((MARGIN, footer_top, CARD_WIDTH - MARGIN, footer_top), fill=PAPER_RULE, width=2)
    footer = _clean(content.footer or "")
    if footer:
        draw.text(
            (MARGIN, footer_top + 20),
            footer,
            font=ImageFont.truetype(regular_path, 25),
            fill=PAPER_MUTED,
        )
    return image


def _render_band(content: CardContent, title: str, fonts: tuple[str, str]) -> Image.Image:
    """Brand name in a coloured header, colour ribbon at the foot."""
    bold_path, _ = fonts
    image = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), (20, 32, 26))
    draw = ImageDraw.Draw(image)

    header_height = 96
    draw.rectangle((0, 0, CARD_WIDTH, header_height), fill=GREEN)
    brand = _clean(content.brand or "")
    if brand:
        draw.text((56, 26), brand, font=ImageFont.truetype(bold_path, 40), fill=PAPER)
    kicker = _clean(content.kicker or "")
    if kicker:
        kicker_font = ImageFont.truetype(bold_path, 24)
        draw.text(
            (CARD_WIDTH - 56 - kicker_font.getlength(kicker.upper()), 36),
            kicker.upper(),
            font=kicker_font,
            fill=(195, 226, 209),
        )

    max_width = CARD_WIDTH - 112
    title_font, title_lines = _fit_title(title, bold_path, max_width, sizes=(68, 60, 52, 46, 40))
    block = (title_font.size + 16) * len(title_lines)
    y = header_height + (CARD_HEIGHT - header_height - 14 - block) // 2
    for line in title_lines:
        draw.text((56, y), line, font=title_font, fill=TEXT_COLOR)
        y += title_font.size + 16

    ribbon = ((47, 111, 79), (233, 196, 106), (141, 110, 99), (79, 109, 143))
    segment = CARD_WIDTH // len(ribbon)
    for index, color in enumerate(ribbon):
        left = index * segment
        right = CARD_WIDTH if index == len(ribbon) - 1 else left + segment
        draw.rectangle((left, CARD_HEIGHT - 14, right, CARD_HEIGHT), fill=color)
    return image


def _render_minimal(content: CardContent, title: str, fonts: tuple[str, str]) -> Image.Image:
    """Nothing but type: white ground, one very large headline."""
    bold_path, regular_path = fonts
    image = Image.new("RGB", (CARD_WIDTH, CARD_HEIGHT), (255, 255, 255))
    draw = ImageDraw.Draw(image)

    left = 96
    max_width = CARD_WIDTH - left * 2
    title_font, title_lines = _fit_title(title, bold_path, max_width, sizes=(78, 68, 60, 52, 44))
    block = (title_font.size + 8) * len(title_lines)

    kicker = _clean(content.kicker or "")
    footer = _clean(content.footer or "")
    total = block + (60 if kicker else 0) + (68 if footer else 0)
    y = max(80, (CARD_HEIGHT - total) // 2)

    if kicker:
        _draw_tracked(
            draw,
            (left, y),
            kicker.upper(),
            ImageFont.truetype(bold_path, 22),
            (138, 138, 132),
            spacing=2,
        )
        y += 60
    for line in title_lines:
        draw.text((left, y), line, font=title_font, fill=(17, 19, 17))
        y += title_font.size + 8
    if footer:
        draw.text(
            (left, y + 36), footer, font=ImageFont.truetype(regular_path, 24), fill=(138, 138, 132)
        )
    return image


RENDERERS: dict[CardStyle, Callable[[CardContent, str, tuple[str, str]], Image.Image]] = {
    CardStyle.DARK: _render_dark,
    CardStyle.ACCENT_BLOCK: _render_accent_block,
    CardStyle.LIGHT: _render_light,
    CardStyle.BAND: _render_band,
    CardStyle.MINIMAL: _render_minimal,
}


def render_card(
    content: CardContent,
    destination: Path,
    *,
    style: CardStyle | None = None,
    seed: str | None = None,
) -> Path:
    """Draw the card and write it as PNG. Returns the path written.

    Without an explicit ``style`` one is chosen from ``seed`` (or the title),
    so every post gets a layout and the same post always gets the same one.
    """
    title = _clean(content.title)
    if not title:
        raise CardRenderError("card needs a title")

    fonts = _load_fonts()
    chosen = style or style_for(seed or title)
    image = RENDERERS[chosen](content, title, fonts)

    destination.parent.mkdir(parents=True, exist_ok=True)
    image.save(destination, format="PNG", optimize=True)
    logger.info("card.rendered", extra={"file": destination.name, "style": chosen.value})
    return destination
