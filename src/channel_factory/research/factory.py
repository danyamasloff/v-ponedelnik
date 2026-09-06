"""Wiring: build the research service with its providers."""

from __future__ import annotations

from typing import Any

from channel_factory.core.config import Settings
from channel_factory.core.enums import ResearchProviderType
from channel_factory.core.logging import get_logger
from channel_factory.db.session import Database
from channel_factory.providers.llm.client import LlmClient
from channel_factory.providers.llm.gemini import GeminiClient
from channel_factory.research.config import TopicScoreConfig, load_topic_score_config
from channel_factory.research.lexicon import TopicLexicon, load_topic_lexicon
from channel_factory.research.providers.github import GitHubReleasesProvider
from channel_factory.research.providers.llm_search import LlmSearchProvider
from channel_factory.research.providers.official_blog import OfficialBlogProvider
from channel_factory.research.providers.rss import RssProvider
from channel_factory.research.service import ResearchService

logger = get_logger(__name__)


def build_llm_client(database: Database, settings: Settings) -> LlmClient:
    """Anthropic client with the configured spend ceilings."""
    return LlmClient(
        database,
        api_key=settings.anthropic_api_key,
        daily_limit_usd=settings.daily_cost_limit_usd,
        monthly_limit_usd=settings.monthly_cost_limit_usd,
    )


def build_judge_client(database: Database, settings: Settings) -> tuple[Any, str]:
    """Pick the client that scores the subjective components.

    Free capacity is preferred over paid: if a Gemini key is present the free
    tier does the judging, and Anthropic is used only when it is the only
    option. Returns the client and a label for reports.
    """
    if settings.gemini_api_key:
        return (
            GeminiClient(
                database,
                api_key=settings.gemini_api_key,
                daily_request_limit=settings.gemini_daily_request_limit,
                requests_per_minute=settings.gemini_requests_per_minute,
            ),
            "gemini free tier",
        )
    return build_llm_client(database, settings), "anthropic api"


def build_research_service(
    database: Database,
    settings: Settings,
    *,
    llm: LlmClient | None = None,
    score_config: TopicScoreConfig | None = None,
    lexicon: TopicLexicon | None = None,
) -> ResearchService:
    """Assemble every provider the whitelist can reference."""
    client = llm or build_llm_client(database, settings)
    providers = {
        ResearchProviderType.RSS: RssProvider(),
        ResearchProviderType.GITHUB_RELEASES: GitHubReleasesProvider(settings.github_token),
        ResearchProviderType.OFFICIAL_BLOG: OfficialBlogProvider(),
        ResearchProviderType.LLM_SEARCH: LlmSearchProvider(client),
    }
    return ResearchService(
        database,
        providers=providers,
        score_config=score_config or load_topic_score_config(settings.topic_score_config),
        lexicon=lexicon or load_topic_lexicon(settings.topic_lexicon_config),
    )
