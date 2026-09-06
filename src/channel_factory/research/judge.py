"""The LLM half of Topic Score.

Four components are judgements, not measurements: whether a cluster is relevant
to our niche, whether a reader could act on it, whether it fits our audience,
and whether there is enough substance for a real post. They are produced by a
cheap model against a fixed rubric, with a strict schema so a malformed answer
can never reach the database.

If there is no API key, or a cost limit has been reached, the judge returns
``None`` and the four components become unavailable — the score is then built
from the deterministic components alone and the reports say so.
"""

from __future__ import annotations

from typing import Any

from channel_factory.core.enums import AiTaskType
from channel_factory.core.logging import get_logger
from channel_factory.db.models import ResearchCluster
from channel_factory.providers.llm.client import (
    CostLimitError,
    LlmClient,
    MissingApiKeyError,
    ThrottleError,
)

logger = get_logger(__name__)

NICHE_DESCRIPTION = (
    "Канал о практических AI и цифровых навыках для работы, учёбы и "
    "повседневных задач. Аудитория — русскоязычные люди, которые хотят "
    "применять инструменты, а не следить за индустрией."
)

RUBRIC = f"""Ты оцениваешь новость для контент-плана канала.

{NICHE_DESCRIPTION}

Оцени по шкале 0-100 каждый параметр:

relevance — насколько это относится к практическим AI и цифровым навыкам.
  0 = корпоративные новости, финансы компаний, слухи; 100 = конкретный
  инструмент или возможность, которой можно пользоваться.

practical_value — сможет ли читатель что-то сделать после прочтения.
  0 = нечего применить; 100 = готовый рабочий приём или инструмент.

audience_fit — подходит ли это нашей аудитории (не исследователям и не
  инвесторам, а обычным людям и специалистам, применяющим инструменты).

content_potential — хватит ли материала на самостоятельный полезный пост
  с конкретикой, а не на пересказ заголовка.

Оценивай строго. Средняя новость про раунд инвестиций или про партнёрство
двух компаний должна получать низкие оценки по всем параметрам."""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "relevance": {"type": "integer", "minimum": 0, "maximum": 100},
        "practical_value": {"type": "integer", "minimum": 0, "maximum": 100},
        "audience_fit": {"type": "integer", "minimum": 0, "maximum": 100},
        "content_potential": {"type": "integer", "minimum": 0, "maximum": 100},
        "reasoning": {"type": "string", "maxLength": 400},
    },
    "required": [
        "relevance",
        "practical_value",
        "audience_fit",
        "content_potential",
        "reasoning",
    ],
    "additionalProperties": False,
}


class ClusterJudge:
    """Scores the subjective components of one cluster."""

    def __init__(self, llm: LlmClient) -> None:
        self._llm = llm
        self._disabled_reason: str | None = None
        self._throttled = 0

    @property
    def disabled_reason(self) -> str | None:
        return self._disabled_reason

    @property
    def throttled_count(self) -> int:
        """Clusters skipped because the provider was rate limiting."""
        return self._throttled

    async def __call__(self, cluster: ResearchCluster) -> dict[str, float] | None:
        if self._disabled_reason is not None:
            return None

        prompt = (
            f"Заголовок: {cluster.canonical_title}\n"
            f"Тип события: {cluster.event_type.value}\n"
            f"Вендор: {cluster.vendor or '—'}\n"
            f"Источников в кластере: {cluster.item_count}\n"
            f"Максимальный уровень доверия источника: {cluster.max_trust.value}\n"
            f"Краткое описание: {(cluster.canonical_summary or '')[:1500]}"
        )

        try:
            result = await self._llm.structured(
                task=AiTaskType.SCORING,
                system=RUBRIC,
                user=prompt,
                schema=SCHEMA,
                max_tokens=512,
            )
        except MissingApiKeyError as exc:
            # Latch the reason: without a key every further call would fail the
            # same way, and a scoring run should not raise once per cluster.
            self._disabled_reason = str(exc)
            logger.warning("cluster judge disabled", extra={"reason": "missing api key"})
            return None
        except CostLimitError as exc:
            self._disabled_reason = str(exc)
            logger.warning("cluster judge disabled", extra={"reason": "budget exhausted"})
            return None
        except ThrottleError:
            # Deliberately not latched: throttling is temporary, and giving up
            # on the whole run because one call was rate limited would waste
            # the remaining quota entirely.
            self._throttled += 1
            logger.warning("cluster judge throttled; skipping this cluster")
            return None
        except Exception as exc:
            logger.warning(
                "cluster judge call failed",
                extra={"cluster": cluster.cluster_key, "error": f"{type(exc).__name__}"},
            )
            return None

        if not result.data:
            return None
        return {
            key: float(result.data[key])
            for key in ("relevance", "practical_value", "audience_fit", "content_potential")
            if key in result.data
        }
