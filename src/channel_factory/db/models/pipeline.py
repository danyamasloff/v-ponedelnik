"""Observability for autonomous work: pipeline runs and AI spend.

Every autonomous decision has to be explainable after the fact, so each run of
each job leaves a row, and every LLM call leaves a row with its cost. Together
they answer "why did this happen, and what did it cost".
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Index, Integer, Numeric, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from channel_factory.core.enums import AiCallStatus, AiTaskType, PipelineJob, RunStatus
from channel_factory.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin


def _enum(enum_type: type, name: str) -> SAEnum:
    return SAEnum(enum_type, name=name, values_callable=lambda e: [m.value for m in e])


class PipelineRun(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One execution of one pipeline job."""

    __tablename__ = "pipeline_runs"
    __table_args__ = (Index("ix_pipeline_runs_job_created", "job", "created_at"),)

    job: Mapped[PipelineJob] = mapped_column(_enum(PipelineJob, "pipeline_job"), nullable=False)
    status: Mapped[RunStatus] = mapped_column(
        _enum(RunStatus, "run_status"), nullable=False, default=RunStatus.RUNNING
    )
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Job-specific counters (items fetched, clusters created, ...). Kept as JSONB
    # because each job counts different things and adding a column per job would
    # make this table a union of unrelated shapes.
    counters: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False, default=Decimal(0))
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<PipelineRun {self.job} {self.status}>"


class AiGeneration(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One LLM call: what it was for, what it cost, what happened.

    This table is the source of truth for spend — the daily and monthly limits
    are enforced against sums over it, not against a counter that could drift.
    Prompts are not stored: only metadata, so a bug cannot leak volumes of text
    into the database and cost control stays cheap to query.
    """

    __tablename__ = "ai_generations"
    __table_args__ = (
        Index("ix_ai_generations_created", "created_at"),
        Index("ix_ai_generations_task_created", "task_type", "created_at"),
    )

    provider: Mapped[str] = mapped_column(String(64), nullable=False, default="anthropic")
    model: Mapped[str] = mapped_column(String(64), nullable=False)
    task_type: Mapped[AiTaskType] = mapped_column(_enum(AiTaskType, "ai_task_type"), nullable=False)
    status: Mapped[AiCallStatus] = mapped_column(
        _enum(AiCallStatus, "ai_call_status"), nullable=False, default=AiCallStatus.SUCCESS
    )

    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_read_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cache_write_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost_usd: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal(0)
    )
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    batched: Mapped[bool] = mapped_column(nullable=False, default=False)

    pipeline_run_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True, index=True)
    # Deliberately not a foreign key: this is an append-only log that must
    # survive deletion of whatever it referred to.
    related_entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    related_entity_id: Mapped[uuid.UUID | None] = mapped_column(nullable=True)

    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<AiGeneration {self.model} {self.task_type} ${self.estimated_cost_usd}>"
