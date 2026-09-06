"""Gemini adapter for the free tier.

Same surface as the Anthropic client, so the judge does not care which one it
is talking to — that is what the provider abstraction was for.

Two differences that matter and are handled explicitly:

* **Money is not the limiting resource.** On the free tier calls cost nothing,
  so a spend limit protects nothing. The quota is a daily request count, and
  that is what is capped here — counted from ``ai_generations``, the same
  ledger the paid path uses.
* **Prompts on the free tier may be used by Google to improve their products.**
  We send public article titles and summaries, never our own credentials or
  private data, and the trade-off is stated in the docs rather than buried.

The request shape is the documented ``interactions`` endpoint; it is verified
against the official docs rather than recalled, because this API changed.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import func, select

from channel_factory.core.enums import AiCallStatus, AiTaskType
from channel_factory.core.logging import get_logger
from channel_factory.db.models import AiGeneration
from channel_factory.db.session import Database
from channel_factory.providers.llm.client import (
    CostLimitError,
    LlmResult,
    LlmUsage,
    MissingApiKeyError,
    ThrottleError,
)
from channel_factory.providers.llm.pricing import GEMINI_FLASH

logger = get_logger(__name__)

ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/interactions"
REQUEST_TIMEOUT_SECONDS = 60.0

# Google documents 1500 requests/day for Flash on the free tier. The default
# here is deliberately lower: hitting a hard external quota mid-run is worse
# than stopping early on our own terms.
DEFAULT_DAILY_REQUEST_LIMIT = 1000

# The free tier also caps requests per minute. Pacing ourselves is not
# politeness: without it a scoring run fires hundreds of calls in seconds, gets
# throttled, and stops early having scored a fraction of the backlog.
DEFAULT_REQUESTS_PER_MINUTE = 5

# Observed on the live free tier: throttling began well before the documented
# per-minute figure, so pacing alone is not enough and a throttled call is
# retried with backoff before being given up on.
MAX_RETRIES = 3
BACKOFF_SECONDS = (5.0, 15.0, 30.0)


class GeminiClient:
    """Structured generation through the Gemini free tier."""

    def __init__(
        self,
        database: Database,
        *,
        api_key: str | None,
        model: str = GEMINI_FLASH,
        daily_request_limit: int = DEFAULT_DAILY_REQUEST_LIMIT,
        requests_per_minute: int = DEFAULT_REQUESTS_PER_MINUTE,
    ) -> None:
        self._database = database
        self._api_key = api_key
        self._model = model
        self._daily_request_limit = daily_request_limit
        self._min_interval = 60.0 / max(requests_per_minute, 1)
        self._last_request_at: float | None = None
        self._pace_lock = asyncio.Lock()

    async def _pace(self) -> None:
        """Hold the configured requests-per-minute rate."""
        async with self._pace_lock:
            if self._last_request_at is not None:
                elapsed = time.monotonic() - self._last_request_at
                if elapsed < self._min_interval:
                    await asyncio.sleep(self._min_interval - elapsed)
            self._last_request_at = time.monotonic()

    async def requests_today(self) -> int:
        """Calls made today, from the same ledger as the paid path."""
        day_start = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        async with self._database.session() as session:
            return (
                await session.execute(
                    select(func.count())
                    .select_from(AiGeneration)
                    .where(
                        AiGeneration.created_at >= day_start,
                        AiGeneration.model == self._model,
                        AiGeneration.status == AiCallStatus.SUCCESS,
                    )
                )
            ).scalar_one()

    async def check_limits(self) -> None:
        """Raise when the daily free quota is spent."""
        if not self._api_key:
            raise MissingApiKeyError(
                "GEMINI_API_KEY is not set. Get a free key at aistudio.google.com; "
                "no credit card is required."
            )
        used = await self.requests_today()
        if used >= self._daily_request_limit:
            raise CostLimitError(
                "daily quota",
                Decimal(used),
                Decimal(self._daily_request_limit),
                unit="requests",
            )

    async def _record(
        self,
        *,
        task: AiTaskType,
        status: AiCallStatus,
        usage: LlmUsage | None,
        error: str | None = None,
    ) -> None:
        async with self._database.session() as session:
            session.add(
                AiGeneration(
                    provider="google",
                    model=self._model,
                    task_type=task,
                    status=status,
                    input_tokens=usage.input_tokens if usage else 0,
                    output_tokens=usage.output_tokens if usage else 0,
                    estimated_cost_usd=Decimal(0),
                    latency_ms=usage.latency_ms if usage else None,
                    error_message=error[:2000] if error else None,
                )
            )
            await session.commit()

    async def structured(
        self,
        *,
        task: AiTaskType,
        system: str,
        user: str,
        schema: dict[str, Any],
        max_tokens: int = 2048,
        pipeline_run_id: Any = None,
    ) -> LlmResult:
        """Return schema-valid JSON from the free-tier model.

        The endpoint has no separate system field, so the rubric is prepended
        to the prompt — the documented shape, not an invented one.
        """
        await self.check_limits()

        payload = {
            "model": self._model,
            "input": f"{system}\n\n---\n\n{user}",
            "response_format": {
                "type": "text",
                "mime_type": "application/json",
                "schema": schema,
            },
        }
        headers = {"x-goog-api-key": self._api_key, "Content-Type": "application/json"}

        started = time.perf_counter()
        response = None
        last_error = ""

        # 429 and 5xx are both transient here: the free tier throttles sooner
        # than documented, and a single hiccup must not cost us the cluster.
        for attempt in range(MAX_RETRIES + 1):
            await self._pace()
            try:
                async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
                    response = await client.post(ENDPOINT, headers=headers, json=payload)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                response = None
            else:
                if response.status_code < 400:
                    break
                last_error = f"HTTP {response.status_code}: {response.text[:300]}"
                if response.status_code != 429 and response.status_code < 500:
                    break

            if attempt < MAX_RETRIES:
                delay = BACKOFF_SECONDS[min(attempt, len(BACKOFF_SECONDS) - 1)]
                logger.warning(
                    "gemini call retrying",
                    extra={"attempt": attempt + 1, "delay_s": delay, "reason": last_error[:80]},
                )
                await asyncio.sleep(delay)

        elapsed_ms = int((time.perf_counter() - started) * 1000)

        if response is None or response.status_code >= 400:
            throttled = response is not None and response.status_code == 429
            await self._record(
                task=task,
                status=AiCallStatus.ERROR,
                usage=None,
                error=last_error,
            )
            if throttled:
                # Temporary, and explicitly not a budget problem: the caller
                # skips this cluster and keeps going.
                raise ThrottleError(
                    f"gemini throttled after {MAX_RETRIES + 1} attempts: {last_error}"
                )
            raise RuntimeError(f"gemini request failed: {last_error}")

        body = response.json()
        usage_raw = body.get("usage") or {}
        usage = LlmUsage(
            model=self._model,
            input_tokens=int(usage_raw.get("total_input_tokens", 0) or 0),
            output_tokens=int(usage_raw.get("total_output_tokens", 0) or 0),
            cost_usd=Decimal(0),
            latency_ms=elapsed_ms,
        )
        await self._record(task=task, status=AiCallStatus.SUCCESS, usage=usage)

        text = _extract_text(body)
        data: dict[str, Any] | None = None
        if text:
            try:
                parsed = json.loads(text)
                data = parsed if isinstance(parsed, dict) else None
            except json.JSONDecodeError:
                # The schema is enforced by the API, but a malformed body must
                # degrade to "no judgement" rather than poison the database.
                logger.warning("gemini returned non-JSON despite a response schema")

        return LlmResult(data=data, text=text or "", usage=usage, raw_blocks=[])


def _extract_text(body: dict[str, Any]) -> str | None:
    """Pull the model output out of ``steps[].content[].text``."""
    for step in body.get("steps") or []:
        if step.get("type") != "model_output":
            continue
        for block in step.get("content") or []:
            if block.get("type") == "text" and block.get("text"):
                return str(block["text"])
    return None
