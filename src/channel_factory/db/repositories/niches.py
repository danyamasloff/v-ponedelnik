"""Repository for stored niche scores."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from channel_factory.db.models import Niche, NicheScore, NicheScoreRun


class NicheScoreRepository:
    """Persistence and retrieval of scoring runs."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    def add_run(self, run: NicheScoreRun) -> None:
        self._session.add(run)

    async def latest_run(self, *, score_version: str | None = None) -> NicheScoreRun | None:
        """Most recent run, optionally restricted to one formula version."""
        query = select(NicheScoreRun).order_by(NicheScoreRun.created_at.desc()).limit(1)
        if score_version:
            query = query.where(NicheScoreRun.score_version == score_version)
        return (await self._session.execute(query)).scalar_one_or_none()

    async def scores_for_run(self, run_id) -> list[tuple[NicheScore, Niche]]:
        """Scores of one run with their niches, ranked; unscored niches last."""
        result = await self._session.execute(
            select(NicheScore, Niche)
            .join(Niche, Niche.id == NicheScore.niche_id)
            .where(NicheScore.run_id == run_id)
            .options(selectinload(NicheScore.run))
            .order_by(NicheScore.rank.nulls_last(), Niche.name)
        )
        return [(score, niche) for score, niche in result.all()]

    async def list_runs(self, limit: int = 20) -> list[NicheScoreRun]:
        result = await self._session.execute(
            select(NicheScoreRun).order_by(NicheScoreRun.created_at.desc()).limit(limit)
        )
        return list(result.scalars().all())
