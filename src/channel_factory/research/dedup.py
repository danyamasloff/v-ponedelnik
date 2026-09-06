"""Deduplication and clustering: many reports, one event.

Four layers, cheapest first, each applied only to what the previous one left
undecided:

1. **Canonical URL** — the same address is the same document.
2. **Content fingerprint** — the same text republished elsewhere.
3. **Entity + time window** — the same ``vendor:product:version`` announced
   within a few days is one release, however many outlets covered it.
4. **SimHash** — near-identical wording that layers 1-3 missed.

Vector embeddings are deliberately absent: they would require a separate model
stack for a job that these layers already do at our volumes. Add them only with
measurements showing they are needed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta

from channel_factory.core.enums import EventType, MatchMethod
from channel_factory.research.canonical import (
    hamming_distance,
    normalize_text,
    simhash,
    tokenize,
)

DEFAULT_TIME_WINDOW_HOURS = 72
DEFAULT_SIMHASH_DISTANCE = 6

# SimHash over a two-word text is noise, not similarity. Real GitHub releases
# titled "stable" or "v3.4.0" were merged into a single cluster before this
# floor existed, so texts with fewer meaningful tokens skip that layer and fall
# back to their entity key, which still separates them by version.
MIN_TOKENS_FOR_SIMHASH = 5

# Vendors we care about. Recognising them turns "OpenAI ships X" and "X arrives
# from OpenAI" into the same cluster key.
VENDOR_KEYWORDS: dict[str, tuple[str, ...]] = {
    "openai": ("openai", "chatgpt", "gpt-4", "gpt-5", "sora", "dall-e", "codex"),
    "anthropic": ("anthropic", "claude"),
    "google": ("google", "gemini", "deepmind", "workspace", "notebooklm"),
    "microsoft": ("microsoft", "copilot", "azure", "office 365", "microsoft 365"),
    "github": ("github",),
    "cursor": ("cursor",),
    "notion": ("notion",),
    "zapier": ("zapier",),
    "ollama": ("ollama",),
    "huggingface": ("hugging face", "huggingface"),
    "n8n": ("n8n",),
    "meta": ("meta ai", "llama"),
    "mistral": ("mistral",),
    "perplexity": ("perplexity",),
    "figma": ("figma",),
    "canva": ("canva",),
}

_VERSION_RE = re.compile(r"\bv?(\d+(?:\.\d+){1,3})\b")

# Words that are never a product name, however they sit in a headline.
_PRODUCT_STOPWORDS = frozenset(
    {"and", "the", "for", "with", "now", "new", "can", "you", "your", "our", "its", "все", "уже"}
)

_EVENT_KEYWORDS: tuple[tuple[EventType, tuple[str, ...]], ...] = (
    (EventType.RELEASE, ("release", "released", "launch", "launches", "introducing",
                         "релиз", "запуск", "представил", "выпустил")),
    (EventType.UPDATE, ("update", "updates", "changelog", "improvement", "now available",
                        "обновление", "обновил")),
    (EventType.NEW_TOOL, ("new tool", "open source", "open-source", "инструмент")),
    (EventType.RESEARCH, ("research", "paper", "benchmark", "исследование")),
    (EventType.GUIDE, ("how to", "guide", "tutorial", "tips", "гайд", "инструкция", "как ")),
    (EventType.INCIDENT, ("outage", "incident", "vulnerability", "breach", "сбой", "уязвимость")),
)


@dataclass(frozen=True)
class ItemSignature:
    """Everything clustering needs to know about one item."""

    item_id: str
    url_hash: str
    content_fingerprint: str
    title: str
    summary: str | None
    simhash: int
    vendor: str | None
    product: str | None
    version: str | None
    event_type: EventType
    event_at: datetime
    token_count: int = 0
    # A product name the provider supplied, as opposed to one guessed from the
    # title. Only the stated one is trustworthy enough to define identity.
    explicit_product: str | None = None

    @property
    def simhash_is_reliable(self) -> bool:
        """Whether this text has enough substance for similarity matching."""
        return self.token_count >= MIN_TOKENS_FOR_SIMHASH


@dataclass(frozen=True)
class ClusterMatch:
    """How an item was attached to a cluster."""

    cluster_key: str
    method: MatchMethod
    similarity: float | None = None


def detect_vendor(text: str) -> str | None:
    """Best-effort vendor from the title and summary."""
    normalized = normalize_text(text)
    for vendor, keywords in VENDOR_KEYWORDS.items():
        if any(keyword in normalized for keyword in keywords):
            return vendor
    return None


def detect_version(text: str) -> str | None:
    """First version-looking token, e.g. ``4.5`` in "Claude Haiku 4.5"."""
    match = _VERSION_RE.search(text or "")
    return match.group(1) if match else None


def detect_event_type(text: str) -> EventType:
    """Classify the kind of event from wording. Deterministic, no LLM needed."""
    normalized = normalize_text(text)
    for event_type, keywords in _EVENT_KEYWORDS:
        if any(keyword in normalized for keyword in keywords):
            return event_type
    return EventType.OTHER


def detect_product(text: str, vendor: str | None) -> str | None:
    """A coarse product token: the first capitalised word after the vendor."""
    if not text:
        return None
    normalized = normalize_text(text)
    if vendor:
        for keyword in VENDOR_KEYWORDS.get(vendor, ()):
            index = normalized.find(keyword)
            if index >= 0:
                tail = normalized[index + len(keyword) :].strip()
                words = [
                    word
                    for word in re.split(r"[^0-9a-zа-яё.]+", tail)
                    # Skip filler like "and"/"for": a product token of two
                    # letters produces keys such as "google:and:-" that group
                    # unrelated stories together.
                    if len(word) >= 3 and word not in _PRODUCT_STOPWORDS
                ]
                if words:
                    return words[0][:64]
    return None


def build_signature(
    *,
    item_id: str,
    url_hash: str,
    content_fingerprint: str,
    title: str,
    summary: str | None,
    event_at: datetime,
    version_hint: str | None = None,
    product_hint: str | None = None,
) -> ItemSignature:
    """Derive all clustering signals from an item.

    Hints come from the provider and win over anything inferred from the text:
    a GitHub release knows its own tag, while its title may be just "stable".
    Guessing from prose when the source states the fact outright is how
    consecutive releases end up merged.
    """
    haystack = f"{title} {summary or ''}"
    vendor = detect_vendor(haystack)
    return ItemSignature(
        item_id=item_id,
        url_hash=url_hash,
        content_fingerprint=content_fingerprint,
        title=title,
        summary=summary,
        simhash=simhash(haystack),
        vendor=vendor,
        product=product_hint or detect_product(title, vendor),
        version=(detect_version(version_hint) if version_hint else None)
        or version_hint
        or detect_version(title),
        event_type=detect_event_type(haystack),
        event_at=event_at,
        token_count=len(tokenize(haystack)),
        explicit_product=product_hint,
    )


# An announcement is reported with whatever verb the outlet prefers: one
# publication "introduces" what another says "ships" or is "now available".
# Only genuinely different kinds of material get their own identity bucket —
# everything else, including wording we do not recognise, is an announcement.
# Defaulting the unknown case to its own bucket would split one event in two
# every time an outlet used an unfamiliar verb.
_DISTINCT_TYPES = frozenset({EventType.GUIDE, EventType.RESEARCH, EventType.INCIDENT})


def _identity_bucket(event_type: EventType) -> str:
    return event_type.value.lower() if event_type in _DISTINCT_TYPES else "announcement"


def entity_cluster_key(signature: ItemSignature) -> str | None:
    """Stable key for "same thing, same week", or ``None`` if too vague.

    A vendor alone is not enough — "OpenAI comments on the industry" would
    swallow every unrelated OpenAI story — so identity additionally needs a
    version, or a product name the *provider* stated. A product guessed from
    the wording is not used here: the guess differs between "Introducing Gemini
    3.8" and "Google launches Gemini 3.8", which would split one event in two.
    """
    if not signature.vendor:
        return None
    if not (signature.version or signature.explicit_product):
        return None
    parts = [
        signature.vendor,
        signature.explicit_product or "-",
        signature.version or "-",
        _identity_bucket(signature.event_type),
        signature.event_at.strftime("%Y-%W"),
    ]
    return ":".join(parts).lower()


def fallback_cluster_key(signature: ItemSignature) -> str:
    """Key for an item with no recognisable entity: its own address.

    This key is only ever used after matching has already decided the item
    belongs to no existing cluster, so it must be **unique**. Deriving it from
    the SimHash instead looked reasonable and silently undid that decision:
    two releases with byte-identical changelogs got the same key and were
    re-merged by the key lookup, right after the matcher had separated them.
    """
    return f"item:{signature.url_hash[:32]}"


def _contradicts(left: ItemSignature, right: ItemSignature) -> bool:
    """Whether an explicit identifier proves these are different events.

    Consecutive releases of the same project share almost all of their text —
    a changelog reads the same from 2.37.6 to 2.37.7 — so textual similarity
    alone merged ten separate n8n releases into one cluster. An explicit
    version or vendor is a stronger signal than fuzzy similarity, so a mismatch
    vetoes the SimHash layer rather than being outvoted by it.
    """
    if left.version and right.version and left.version != right.version:
        return True
    return bool(left.vendor and right.vendor and left.vendor != right.vendor)


def match_cluster(
    signature: ItemSignature,
    candidates: list[ItemSignature],
    *,
    window_hours: int = DEFAULT_TIME_WINDOW_HOURS,
    max_distance: int = DEFAULT_SIMHASH_DISTANCE,
) -> tuple[ItemSignature, MatchMethod, float | None] | None:
    """Find the existing item this one duplicates, if any.

    Returns the matched signature, the layer that matched, and a similarity in
    ``0..1`` where the layer produces one.
    """
    window = timedelta(hours=window_hours)

    for other in candidates:
        if other.url_hash == signature.url_hash:
            return other, MatchMethod.URL, 1.0

    for other in candidates:
        # Identical text is strong evidence of the same document, but not
        # unconditional: two releases can carry byte-identical boilerplate, and
        # a periodic post ("the news we announced this month") repeats verbatim
        # every cycle. So an explicit version mismatch, or a gap wider than the
        # window, still separates them.
        if other.content_fingerprint != signature.content_fingerprint:
            continue
        if _contradicts(signature, other):
            continue
        if abs(other.event_at - signature.event_at) > window:
            continue
        return other, MatchMethod.FINGERPRINT, 1.0

    key = entity_cluster_key(signature)
    if key is not None:
        for other in candidates:
            if abs(other.event_at - signature.event_at) > window:
                continue
            if entity_cluster_key(other) == key:
                return other, MatchMethod.ENTITY, None

    best: tuple[ItemSignature, int] | None = None
    if not signature.simhash_is_reliable:
        return None
    for other in candidates:
        if not other.simhash_is_reliable:
            continue
        if abs(other.event_at - signature.event_at) > window:
            continue
        if _contradicts(signature, other):
            continue
        distance = hamming_distance(signature.simhash, other.simhash)
        if distance <= max_distance and (best is None or distance < best[1]):
            best = (other, distance)
    if best is not None:
        matched, distance = best
        return matched, MatchMethod.SIMHASH, round(1 - distance / 64, 4)

    return None
