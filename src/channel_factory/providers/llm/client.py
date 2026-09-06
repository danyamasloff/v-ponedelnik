"""Anthropic LLM client with routing, spend accounting and hard cost limits.

Every call is recorded in ``ai_generations`` **before** its result is used, and
every call is checked against the daily and monthly limits **before** it is
made. A runaway loop therefore stops at the limit instead of draining the API
balance, and the spend figure always comes from the same table the limits are
enforced against.

Structured output is obtained through a single ``strict: true`` tool rather
than free-form JSON parsing: the API then guarantees the arguments validate
against the schema, so a malformed model response cannot reach the database.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import anthropic
from sqlalchemy import func, select

from channel_factory.core.enums import AiCallStatus, AiTaskType
from channel_factory.core.logging import get_logger
from channel_factory.db.models import AiGeneration
from channel_factory.db.session import Database
from channel_factory.providers.llm.pricing import estimate_cost, model_for

logger = get_logger(__name__)

OUTPUT_TOOL_NAME = "emit_result"


class LlmError(Exception):
    """Base class for LLM layer failures."""


class CostLimitError(LlmError):
    """Raised when a budget is exhausted. Not transient: stop for today.

    ``unit`` exists because free tiers are limited by request count rather than
    money, and printing a request count with a dollar sign is how an operator
    ends up chasing a spending problem that does not exist.
    """

    def __init__(
        self, scope: str, spent: Decimal, limit: Decimal, *, unit: str = "USD"
    ) -> None:
        if unit == "USD":
            detail = f"${spent:.4f} of ${limit:.2f} already spent"
        else:
            detail = f"{int(spent)} of {int(limit)} {unit} already used"
        super().__init__(f"{scope} limit reached: {detail}")
        self.scope = scope
        self.spent = spent
        self.limit = limit
        self.unit = unit


class ThrottleError(LlmError):
    """The provider asked us to slow down.

    Deliberately distinct from :class:`CostLimitError`: a throttle is temporary
    and the next cluster may well succeed, so it must not disable judging for
    the rest of the run.
    """


class MissingApiKeyError(LlmError):
    """Raised when no Anthropic credentials are configured."""


@dataclass
class LlmUsage:
    """Token and cost accounting for one call."""

    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: Decimal = Decimal(0)
    latency_ms: int = 0


@dataclass
class LlmResult:
    """Result of one call plus what it cost."""

    data: dict[str, Any] | None
    text: str
    usage: LlmUsage
    raw_blocks: list[Any] = field(default_factory=list)


class LlmClient:
    """Routes tasks to models, enforces spend limits, records every call."""

    def __init__(
        self,
        database: Database,
        *,
        api_key: str | None,
        daily_limit_usd: Decimal,
        monthly_limit_usd: Decimal,
    ) -> None:
        self._database = database
        self._api_key = api_key
        self._daily_limit = daily_limit_usd
        self._monthly_limit = monthly_limit_usd
        self._client: anthropic.AsyncAnthropic | None = None

    @property
    def client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            if not self._api_key:
                raise MissingApiKeyError(
                    "ANTHROPIC_API_KEY is not set. The research engine needs its own API "
                    "key: a Claude subscription does not provide one."
                )
            self._client = anthropic.AsyncAnthropic(api_key=self._api_key)
        return self._client

    async def spend(self, *, since: datetime) -> Decimal:
        """Total recorded spend since a point in time."""
        async with self._database.session() as session:
            total = (
                await session.execute(
                    select(func.coalesce(func.sum(AiGeneration.estimated_cost_usd), 0)).where(
                        AiGeneration.created_at >= since
                    )
                )
            ).scalar_one()
        return Decimal(total)

    async def check_limits(self) -> None:
        """Raise :class:`CostLimitError` if either limit is already reached."""
        now = datetime.now(UTC)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = day_start.replace(day=1)

        daily = await self.spend(since=day_start)
        if daily >= self._daily_limit:
            raise CostLimitError("daily", daily, self._daily_limit)

        monthly = await self.spend(since=month_start)
        if monthly >= self._monthly_limit:
            raise CostLimitError("monthly", monthly, self._monthly_limit)

    async def _record(
        self,
        *,
        model: str,
        task: AiTaskType,
        status: AiCallStatus,
        usage: LlmUsage | None,
        pipeline_run_id: Any = None,
        error: str | None = None,
    ) -> None:
        async with self._database.session() as session:
            session.add(
                AiGeneration(
                    model=model,
                    task_type=task,
                    status=status,
                    input_tokens=usage.input_tokens if usage else 0,
                    output_tokens=usage.output_tokens if usage else 0,
                    cache_read_tokens=usage.cache_read_tokens if usage else 0,
                    cache_write_tokens=usage.cache_write_tokens if usage else 0,
                    estimated_cost_usd=usage.cost_usd if usage else Decimal(0),
                    latency_ms=usage.latency_ms if usage else None,
                    pipeline_run_id=pipeline_run_id,
                    error_message=error[:2000] if error else None,
                )
            )
            await session.commit()

    def _usage_from(self, model: str, response: Any, elapsed_ms: int) -> LlmUsage:
        raw = response.usage
        usage = LlmUsage(
            model=model,
            input_tokens=getattr(raw, "input_tokens", 0) or 0,
            output_tokens=getattr(raw, "output_tokens", 0) or 0,
            cache_read_tokens=getattr(raw, "cache_read_input_tokens", 0) or 0,
            cache_write_tokens=getattr(raw, "cache_creation_input_tokens", 0) or 0,
            latency_ms=elapsed_ms,
        )
        usage.cost_usd = estimate_cost(
            model,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            cache_read_tokens=usage.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens,
        )
        return usage

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
        """Call the routed model and return schema-valid JSON.

        The system prompt is marked cacheable: it is the stable rubric shared by
        every call of a task, while the volatile item text goes in the user
        message after it.
        """
        await self.check_limits()
        model = model_for(task)
        tool = {
            "name": OUTPUT_TOOL_NAME,
            "description": "Return the result in the required structure.",
            "input_schema": schema,
            "strict": True,
        }

        started = time.perf_counter()
        try:
            response = await self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                system=[
                    {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
                ],
                messages=[{"role": "user", "content": user}],
                tools=[tool],
                tool_choice={"type": "tool", "name": OUTPUT_TOOL_NAME},
            )
        except Exception as exc:
            await self._record(
                model=model,
                task=task,
                status=AiCallStatus.ERROR,
                usage=None,
                pipeline_run_id=pipeline_run_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        usage = self._usage_from(model, response, elapsed_ms)
        await self._record(
            model=model,
            task=task,
            status=AiCallStatus.SUCCESS,
            usage=usage,
            pipeline_run_id=pipeline_run_id,
        )

        data: dict[str, Any] | None = None
        text_parts: list[str] = []
        for block in response.content:
            if block.type == "tool_use" and block.name == OUTPUT_TOOL_NAME:
                data = dict(block.input)
            elif block.type == "text":
                text_parts.append(block.text)

        return LlmResult(
            data=data, text="\n".join(text_parts), usage=usage, raw_blocks=list(response.content)
        )

    async def web_search(
        self,
        *,
        query: str,
        max_uses: int = 3,
        max_tokens: int = 4096,
        allowed_domains: list[str] | None = None,
        pipeline_run_id: Any = None,
    ) -> LlmResult:
        """Run a web search through the server-side tool and return its results.

        The search results themselves are read out of the
        ``web_search_tool_result`` blocks rather than re-derived from the
        model's prose, so URLs and titles come from the tool, not from a
        paraphrase.
        """
        await self.check_limits()
        task = AiTaskType.SEARCH
        model = model_for(task)

        search_tool: dict[str, Any] = {
            "type": "web_search_20260209",
            "name": "web_search",
            "max_uses": max_uses,
        }
        if allowed_domains:
            search_tool["allowed_domains"] = allowed_domains

        started = time.perf_counter()
        try:
            response = await self.client.messages.create(
                model=model,
                max_tokens=max_tokens,
                messages=[{"role": "user", "content": query}],
                tools=[search_tool],
            )
        except Exception as exc:
            await self._record(
                model=model,
                task=task,
                status=AiCallStatus.ERROR,
                usage=None,
                pipeline_run_id=pipeline_run_id,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

        elapsed_ms = int((time.perf_counter() - started) * 1000)
        usage = self._usage_from(model, response, elapsed_ms)
        await self._record(
            model=model,
            task=task,
            status=AiCallStatus.SUCCESS,
            usage=usage,
            pipeline_run_id=pipeline_run_id,
        )

        text_parts = [block.text for block in response.content if block.type == "text"]
        return LlmResult(
            data=None,
            text="\n".join(text_parts),
            usage=usage,
            raw_blocks=list(response.content),
        )

    async def note_blocked(self, task: AiTaskType, reason: str) -> None:
        """Record that a call was refused by the cost guard."""
        await self._record(
            model=model_for(task),
            task=task,
            status=AiCallStatus.BLOCKED_BY_COST_LIMIT,
            usage=None,
            error=reason,
        )
        logger.warning("llm call blocked by cost limit", extra={"task": task.value})


def extract_search_results(blocks: list[Any]) -> list[dict[str, Any]]:
    """Pull ``{url, title, page_age}`` out of web_search_tool_result blocks.

    Written defensively: the block shape is owned by the API, so unexpected
    fields are skipped rather than allowed to crash a scheduled run.
    """
    results: list[dict[str, Any]] = []
    for block in blocks:
        if getattr(block, "type", None) != "web_search_tool_result":
            continue
        content = getattr(block, "content", None)
        if not isinstance(content, list):
            # An error result is an object, not a list — nothing to harvest.
            continue
        for entry in content:
            url = getattr(entry, "url", None)
            if not url:
                continue
            results.append(
                {
                    "url": url,
                    "title": getattr(entry, "title", None) or url,
                    "page_age": getattr(entry, "page_age", None),
                }
            )
    return results
