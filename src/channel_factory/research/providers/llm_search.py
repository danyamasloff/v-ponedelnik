"""LLM-driven scouting through the Anthropic server-side web search tool.

This is the replacement for the "AI trend scout" idea: the same judgement, but
running inside our own process, so results land in PostgreSQL with a schema
instead of arriving as prose from a cloud agent that cannot reach the database.

Results always start at UNKNOWN trust. Promotion happens later, and only when
the result's domain resolves to a source that is already whitelisted — a search
hit never grants itself authority.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from channel_factory.core.enums import ResearchProviderType
from channel_factory.core.logging import get_logger
from channel_factory.providers.llm.client import (
    CostLimitError,
    LlmClient,
    extract_search_results,
)
from channel_factory.research.providers.base import (
    ProviderError,
    RawResearchItem,
    SourceConfig,
)

logger = get_logger(__name__)

DEFAULT_MAX_RESULTS = 8

SEARCH_PREAMBLE = (
    "Найди свежие публикации о практическом применении AI и цифровых "
    "инструментов в работе, учёбе и повседневных задачах. Нужны конкретные "
    "релизы, обновления и инструменты, а не общие рассуждения. Запрос: "
)


class LlmSearchProvider:
    """Finds items that no whitelisted feed carries."""

    provider_type = ResearchProviderType.LLM_SEARCH

    def __init__(self, llm: LlmClient) -> None:
        self._llm = llm

    async def fetch(self, source: SourceConfig) -> list[RawResearchItem]:
        config = source.config or {}
        queries = config.get("queries") or []
        if not queries:
            raise ProviderError(f"source {source.key!r} has no queries configured")
        limit = int(config.get("max_results_per_query", DEFAULT_MAX_RESULTS))

        items: list[RawResearchItem] = []
        seen: set[str] = set()

        for query in queries:
            try:
                result = await self._llm.web_search(query=SEARCH_PREAMBLE + query)
            except CostLimitError:
                # Budget exhaustion is an expected operating state, not a source
                # failure: stop searching and keep whatever was already found.
                logger.warning("llm search stopped: cost limit reached")
                break
            except Exception as exc:
                raise ProviderError(f"web search failed for {query!r}: {exc}") from exc

            for entry in extract_search_results(result.raw_blocks)[:limit]:
                url = entry["url"]
                if url in seen:
                    continue
                seen.add(url)
                items.append(
                    RawResearchItem(
                        external_id=url,
                        url=url,
                        title=str(entry.get("title") or url)[:1024],
                        summary=None,
                        content_excerpt=None,
                        published_at=None,
                        publisher=urlsplit(url).netloc,
                        raw_metadata={
                            "query": query,
                            "page_age": entry.get("page_age"),
                            "discovered_by": "llm_web_search",
                        },
                    )
                )
        return items
