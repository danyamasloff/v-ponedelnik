"""Computed niche scores and the runs that produced them.

Scores are append-only: every ``analyze-niches`` run inserts new rows and never
updates old ones. The formula version is stored alongside the value, so a
report produced months ago stays interpretable after the formula changes.

The run table exists so a ranking can be rendered later exactly as it was
computed: it captures the dataset the run stood on and the weights actually
used, which per-niche rows have no place for.
"""

from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Boolean, Date, ForeignKey, Integer, Numeric, String
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from channel_factory.core.enums import Platform
from channel_factory.db.base import Base, CreatedAtMixin, UUIDPrimaryKeyMixin
from channel_factory.db.models.niche import Niche


class NicheScoreRun(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One execution of the scoring pipeline."""

    __tablename__ = "niche_score_runs"

    score_version: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # NULL means "all platforms". A single brand launches on Telegram and MAX at
    # once, so a niche has to be comparable per platform and across both.
    platform: Mapped[Platform | None] = mapped_column(
        SAEnum(
            Platform,
            name="platform",
            values_callable=lambda e: [m.value for m in e],
            create_type=False,
        ),
        nullable=True,
        index=True,
    )
    as_of_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    niches_scored: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    niches_skipped: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    low_confidence: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Snapshot of the dataset and of the configuration used, so a stored run is
    # fully self-describing and reports never drift from the scores.
    dataset: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    scores: Mapped[list[NicheScore]] = relationship(
        back_populates="run", cascade="all, delete-orphan", passive_deletes=True
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<NicheScoreRun {self.score_version} at {self.created_at}>"


class NicheScore(Base, UUIDPrimaryKeyMixin, CreatedAtMixin):
    """One niche's score from one scoring run."""

    __tablename__ = "niche_scores"

    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("niche_score_runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    niche_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("niches.id", ondelete="CASCADE"), nullable=False, index=True
    )
    score_version: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2), nullable=True)
    rank: Mapped[int | None] = mapped_column(Integer, nullable=True)
    channels_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    insufficient_data: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Full component breakdown (value, weight, source, availability) so any past
    # score can be explained without re-running the computation.
    components: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    metrics: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)

    run: Mapped[NicheScoreRun] = relationship(back_populates="scores")
    niche: Mapped[Niche] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debugging helper
        return f"<NicheScore {self.score_version} niche={self.niche_id} score={self.score}>"
