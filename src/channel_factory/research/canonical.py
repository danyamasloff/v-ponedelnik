"""URL canonicalization, content fingerprints and SimHash.

Three different questions need three different keys, and conflating them is the
classic source of both missed duplicates and lost updates:

* **Is this the same address?** -> ``canonical_url`` / ``url_hash``.
* **Did the content at that address change?** -> ``content_fingerprint``.
* **Is this text near-identical to that text?** -> ``simhash``.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

__all__ = [
    "canonicalize_url",
    "content_fingerprint",
    "hamming_distance",
    "normalize_text",
    "simhash",
    "to_signed_64",
    "tokenize",
    "url_hash",
]

# Query parameters that identify the campaign that brought you to a page, never
# the page itself. Dropping them collapses the same article shared five ways.
_TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "ref",
        "ref_src",
        "referrer",
        "source",
        "spm",
        "yclid",
        "_hsenc",
        "_hsmi",
    }
)

_DEFAULT_PORTS = {"http": "80", "https": "443"}

_STOPWORDS = frozenset(
    {
        "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have",
        "in", "is", "it", "its", "of", "on", "or", "that", "the", "to", "was", "were",
        "will", "with", "you", "your", "we", "our", "new", "now",
        "и", "в", "во", "не", "что", "он", "на", "я", "с", "со", "как", "а", "то",
        "все", "она", "так", "его", "но", "да", "ты", "к", "у", "же", "вы", "за",
        "бы", "по", "только", "ее", "мне", "было", "вот", "от", "для", "или",
    }
)

_WORD_RE = re.compile(r"[0-9a-zа-яё]+", re.IGNORECASE)


def canonicalize_url(url: str) -> str:
    """Return a stable, comparable form of a URL.

    Lowercases scheme and host, drops ``www.``, default ports, fragments and
    tracking parameters, and sorts what remains so parameter order cannot make
    one page look like two.
    """
    text = (url or "").strip()
    if not text:
        raise ValueError("empty url")
    if "://" not in text:
        text = f"https://{text}"

    parts = urlsplit(text)
    scheme = (parts.scheme or "https").lower()

    host = parts.hostname or ""
    host = host.lower().removeprefix("www.")
    port = parts.port
    netloc = host
    if port and str(port) != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{port}"

    path = parts.path or "/"
    if len(path) > 1:
        path = path.rstrip("/")

    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=False)
        if key.lower() not in _TRACKING_PARAMS and not key.lower().startswith("utm_")
    ]
    query = urlencode(sorted(kept))

    return urlunsplit((scheme, netloc, path, query, ""))


def url_hash(canonical: str) -> str:
    """SHA-256 of a canonical URL — a cross-source duplicate signal, not an id."""
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def normalize_text(value: str | None) -> str:
    """Collapse a text to a comparable form: NFKC, lowercase, single spaces."""
    if not value:
        return ""
    text = unicodedata.normalize("NFKC", value).lower()
    return re.sub(r"\s+", " ", text).strip()


def content_fingerprint(*parts: str | None) -> str:
    """Fingerprint of the content itself.

    A page that keeps its URL but changes its text — the normal case for
    changelogs and documentation — produces a different fingerprint, which is
    how the importer tells "already seen" from "updated".
    """
    joined = "\x1f".join(normalize_text(part) for part in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def tokenize(text: str) -> list[str]:
    """Words worth comparing: no punctuation, no stopwords, no single letters."""
    words = _WORD_RE.findall(normalize_text(text))
    return [word for word in words if len(word) > 1 and word not in _STOPWORDS]


def simhash(text: str, *, bits: int = 64) -> int:
    """64-bit SimHash of a text, as an unsigned integer.

    Near-identical texts differ in only a few bits, so a cheap Hamming distance
    catches "the same announcement, reworded" without any embedding model.
    """
    tokens = tokenize(text)
    if not tokens:
        return 0

    vector = [0] * bits
    for token in tokens:
        digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
        value = int.from_bytes(digest, "big")
        for position in range(bits):
            vector[position] += 1 if value >> position & 1 else -1

    result = 0
    for position in range(bits):
        if vector[position] > 0:
            result |= 1 << position
    return result


def hamming_distance(left: int, right: int) -> int:
    """Number of differing bits between two SimHash values."""
    return ((left ^ right) & 0xFFFFFFFFFFFFFFFF).bit_count()


def to_signed_64(value: int) -> int:
    """Map an unsigned 64-bit value onto PostgreSQL's signed bigint range."""
    value &= 0xFFFFFFFFFFFFFFFF
    return value - 0x10000000000000000 if value >= 0x8000000000000000 else value


def from_signed_64(value: int) -> int:
    """Inverse of :func:`to_signed_64`."""
    return value + 0x10000000000000000 if value < 0 else value
