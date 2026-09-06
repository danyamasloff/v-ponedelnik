"""Choosing who writes the analysis paragraph.

Preference order, and the reason for it:

1. **Ollama**, when a local server answers with the model pulled — free, and
   nothing about the topic leaves this machine.
2. **Gemini free tier**, when a key is configured — also free, but the prompt
   travels to Google.
3. **Nobody.** Drafts then stay skeletons and the publisher refuses them. That
   is the correct outcome: a half-written post must not reach the channel.
"""

from __future__ import annotations

from channel_factory.content.generation import (
    AnalysisGenerator,
    GeminiAnalysisGenerator,
    OllamaGenerator,
)
from channel_factory.core.config import Settings
from channel_factory.core.logging import get_logger
from channel_factory.db.session import Database
from channel_factory.providers.llm.gemini import GeminiClient

logger = get_logger(__name__)


async def build_analysis_generator(
    database: Database, settings: Settings
) -> AnalysisGenerator | None:
    """The best available generator, or ``None`` when there is none."""
    ollama = OllamaGenerator(base_url=settings.ollama_base_url, model=settings.ollama_model)
    if await ollama.available():
        logger.info(
            "content.generator", extra={"backend": "ollama", "model": settings.ollama_model}
        )
        return ollama

    if settings.gemini_api_key:
        logger.info("content.generator", extra={"backend": "gemini"})
        return GeminiAnalysisGenerator(
            GeminiClient(
                database,
                api_key=settings.gemini_api_key,
                daily_request_limit=settings.gemini_daily_request_limit,
                requests_per_minute=settings.gemini_requests_per_minute,
            )
        )

    logger.warning("content.generator.unavailable")
    return None
