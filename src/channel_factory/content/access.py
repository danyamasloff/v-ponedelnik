"""Can the channel's audience open this link?

MAX is a Russian network, and its readers are in Russia. A post that points at
a site they cannot reach without a VPN is worse than a post with no link: the
reader clicks, gets nothing, and learns that our links do not work.

So a link is printed only when the source is declared reachable
(``audience_access: RU`` in the source whitelist). Everything else is credited
by name, and the post has to carry its whole meaning by itself — which is the
right pressure on the writing anyway.

Access is a property of the domain, so it is resolved from the whitelist rather
than stored per row: a source added to the config immediately affects every
post that cites it.
"""

from __future__ import annotations

from functools import lru_cache
from urllib.parse import urlsplit

from channel_factory.core.config import get_settings
from channel_factory.core.logging import get_logger
from channel_factory.research.config import load_source_configs

logger = get_logger(__name__)


def normalize_domain(url: str | None) -> str | None:
    """Host of a URL, without ``www.`` and without a port."""
    if not url:
        return None
    host = urlsplit(url if "//" in url else f"https://{url}").netloc.lower()
    host = host.split("@")[-1].split(":")[0]
    return host[4:] if host.startswith("www.") else host or None


@lru_cache(maxsize=1)
def reachable_domains() -> frozenset[str]:
    """Domains the whitelist marks as reachable without a VPN."""
    try:
        sources = load_source_configs(get_settings().research_sources_config)
    except Exception as exc:  # a broken config must not publish bad links
        logger.warning("access.config_unreadable", extra={"error": str(exc)})
        return frozenset()
    domains = {
        normalize_domain(source.url)
        for source in sources
        if source.audience_access == "RU" and source.url
    }
    return frozenset(domain for domain in domains if domain)


def audience_can_open(url: str | None) -> bool:
    """Whether a link is worth printing for readers of a Russian channel.

    Unknown domains answer ``False``. That is deliberate: the cost of a wrong
    "yes" is a dead link in front of readers, the cost of a wrong "no" is one
    missing link under a post that already says everything it needs to.
    """
    domain = normalize_domain(url)
    if not domain:
        return False
    allowed = reachable_domains()
    # Subdomains of an allowed domain count too (blog.habr.com -> habr.com).
    return any(domain == item or domain.endswith(f".{item}") for item in allowed)
