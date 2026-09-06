"""Choosing who writes the analysis paragraph.

Preference order, and the reason for it:

1. **Ollama**, when a local server answers with the model pulled — free, no
   quota at all, and nothing about the topic leaves this machine.
2. **A configured OpenAI-compatible endpoint** (Qwen via Alibaba Model Studio,
   an OpenRouter ``:free`` model, anything of that shape) — free within its
   own quota, and explicitly chosen by whoever set the key.
3. **Gemini free tier**, when a key is present. Last because its free quota is
   the tightest: it is per model and, measured on 2026-09-06, twenty requests
   a day for gemini-3.8-flash. The client rotates models to stretch that.
4. **Nobody.** Drafts then stay skeletons and the publisher refuses them. That
   is the correct outcome: a half-written post must not reach the channel.
"""

from __future__ import annotations

from channel_factory.content.generation import (
    AnalysisGenerator,
    GeminiAnalysisGenerator,
    OllamaGenerator,
    OpenAICompatibleGenerator,
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

    if settings.content_api_base_url and settings.content_api_key and settings.content_model:
        logger.info(
            "content.generator",
            extra={"backend": "openai-compatible", "model": settings.content_model},
        )
        return OpenAICompatibleGenerator(
            base_url=settings.content_api_base_url,
            api_key=settings.content_api_key.get_secret_value(),
            model=settings.content_model,
        )

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
