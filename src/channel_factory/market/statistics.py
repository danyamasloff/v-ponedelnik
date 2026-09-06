"""Per-niche market statistics computed in PostgreSQL.

These aggregations are written as SQL rather than assembled in Python on
purpose: ``percentile_cont`` and window functions do medians, quartiles and
top-N concentration natively, and the database never has to ship raw rows to
the application just to reduce them.

Robust statistics only — median and quartiles, never the mean. A single huge
channel must not define the niche it happens to sit in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from channel_factory.core.enums import Platform

# Platform scoping. A single brand is launched on Telegram and MAX at once, so
# every aggregate must be answerable for one platform as well as for both, and
# NULL means "both". Compared as text to keep the enum out of the parameter.
_PLATFORM_FILTER = (
    "(CAST(:platform AS text) IS NULL OR mc.platform::text = CAST(:platform AS text))"
)

# The most recent snapshot per channel, optionally as of a chosen date. This is
# the "current state of the market" the analytics work from.
_LATEST_SNAPSHOT_CTE = """
    latest AS (
        SELECT DISTINCT ON (ms.market_channel_id)
            ms.market_channel_id,
            ms.snapshot_date,
            ms.subscribers,
            ms.err,
            ms.predicted_views,
            ms.cpv,
            ms.campaign_price
        FROM market_snapshots ms
        WHERE (CAST(:as_of AS date) IS NULL OR ms.snapshot_date <= CAST(:as_of AS date))
        ORDER BY ms.market_channel_id, ms.snapshot_date DESC
    )
"""

_METRICS_SQL = f"""
WITH {_LATEST_SNAPSHOT_CTE},
    per_channel AS (
        SELECT
            mc.niche_id,
            l.subscribers,
            l.err,
            l.predicted_views,
            l.cpv,
            l.campaign_price,
            CASE
                WHEN l.subscribers > 0 AND l.predicted_views IS NOT NULL
                THEN l.predicted_views::numeric / l.subscribers
            END AS views_to_subscribers
        FROM latest l
        JOIN market_channels mc ON mc.id = l.market_channel_id
        WHERE mc.niche_id IS NOT NULL AND {_PLATFORM_FILTER}
    )
SELECT
    n.id                AS niche_id,
    n.slug              AS slug,
    n.name              AS name,
    count(*)                                                          AS channels_count,
    count(pc.subscribers)                                             AS subscribers_known,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY pc.subscribers)       AS median_subscribers,
    percentile_cont(0.25) WITHIN GROUP (ORDER BY pc.subscribers)      AS p25_subscribers,
    percentile_cont(0.75) WITHIN GROUP (ORDER BY pc.subscribers)      AS p75_subscribers,
    sum(pc.subscribers)                                               AS total_subscribers,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY pc.err)               AS median_err,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY pc.predicted_views)   AS median_views,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY pc.cpv)               AS median_cpv,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY pc.campaign_price)    AS median_campaign_price,
    percentile_cont(0.5) WITHIN GROUP (ORDER BY pc.views_to_subscribers)
                                                                      AS median_views_to_subs
FROM per_channel pc
JOIN niches n ON n.id = pc.niche_id
GROUP BY n.id, n.slug, n.name
ORDER BY n.name
"""

# Share of the niche's subscribers held by its largest channels: the higher the
# share, the harder it is for a newcomer to win attention.
#
# Niches with no more channels than :top_n are excluded (HAVING): there the top
# N *are* the whole niche, so the share is 100% by construction. Reporting that
# as "maximum competition" would penalise a niche for the size of our sample
# rather than for the state of its market, so the value is left NULL and the
# component drops out of the score instead.
_CONCENTRATION_SQL = f"""
WITH {_LATEST_SNAPSHOT_CTE},
    ranked AS (
        SELECT
            mc.niche_id,
            l.subscribers,
            row_number() OVER (
                PARTITION BY mc.niche_id ORDER BY l.subscribers DESC NULLS LAST
            ) AS position
        FROM latest l
        JOIN market_channels mc ON mc.id = l.market_channel_id
        WHERE mc.niche_id IS NOT NULL
          AND l.subscribers IS NOT NULL
          AND {_PLATFORM_FILTER}
    )
SELECT
    niche_id,
    sum(subscribers) FILTER (WHERE position <= :top_n)::numeric
        / NULLIF(sum(subscribers), 0) AS top_share
FROM ranked
GROUP BY niche_id
HAVING count(*) > :top_n
"""

# Growth needs two snapshots of the same channel on different dates. Until a
# second export is imported this query returns nothing, and the growth
# component simply drops out of the score.
_GROWTH_SQL = """
WITH bounds AS (
    SELECT
        ms.market_channel_id,
        min(ms.snapshot_date) AS first_date,
        max(ms.snapshot_date) AS last_date
    FROM market_snapshots ms
    WHERE ms.subscribers IS NOT NULL
      AND (CAST(:as_of AS date) IS NULL OR ms.snapshot_date <= CAST(:as_of AS date))
    GROUP BY ms.market_channel_id
    HAVING min(ms.snapshot_date) < max(ms.snapshot_date)
),
first_value AS (
    SELECT DISTINCT ON (ms.market_channel_id)
        ms.market_channel_id, ms.subscribers
    FROM market_snapshots ms
    JOIN bounds b ON b.market_channel_id = ms.market_channel_id
                 AND b.first_date = ms.snapshot_date
    WHERE ms.subscribers IS NOT NULL
    ORDER BY ms.market_channel_id, ms.created_at
),
last_value AS (
    SELECT DISTINCT ON (ms.market_channel_id)
        ms.market_channel_id, ms.subscribers
    FROM market_snapshots ms
    JOIN bounds b ON b.market_channel_id = ms.market_channel_id
                 AND b.last_date = ms.snapshot_date
    WHERE ms.subscribers IS NOT NULL
    ORDER BY ms.market_channel_id, ms.created_at DESC
)
SELECT
    mc.niche_id,
    percentile_cont(0.5) WITHIN GROUP (
        ORDER BY (lv.subscribers - fv.subscribers)::numeric / NULLIF(fv.subscribers, 0)
    ) AS median_growth_rate,
    count(*) AS channels_with_history
FROM first_value fv
JOIN last_value lv ON lv.market_channel_id = fv.market_channel_id
JOIN market_channels mc ON mc.id = fv.market_channel_id
WHERE mc.niche_id IS NOT NULL
  AND fv.subscribers > 0
  AND (CAST(:platform AS text) IS NULL OR mc.platform::text = CAST(:platform AS text))
GROUP BY mc.niche_id
"""

_DATASET_SQL = f"""
WITH scoped AS (
    SELECT mc.id, mc.niche_id
    FROM market_channels mc
    WHERE {_PLATFORM_FILTER}
),
scoped_snapshots AS (
    SELECT ms.*
    FROM market_snapshots ms
    JOIN scoped s ON s.id = ms.market_channel_id
)
SELECT
    (SELECT count(*) FROM scoped)                                   AS channels,
    (SELECT count(*) FROM scoped WHERE niche_id IS NULL)             AS channels_without_niche,
    (SELECT count(*) FROM scoped_snapshots)                          AS snapshots,
    (SELECT count(DISTINCT snapshot_date) FROM scoped_snapshots)     AS snapshot_dates,
    (SELECT min(snapshot_date) FROM scoped_snapshots)                AS first_date,
    (SELECT max(snapshot_date) FROM scoped_snapshots)                AS last_date,
    (SELECT count(DISTINCT niche_id) FROM scoped WHERE niche_id IS NOT NULL) AS niches,
    (SELECT count(*) FROM direct_imports WHERE status IN ('SUCCESS', 'PARTIAL')) AS imports
"""

_PLATFORM_SQL = f"""
SELECT mc.platform, count(*) AS channels
FROM market_channels mc
WHERE {_PLATFORM_FILTER}
GROUP BY mc.platform
ORDER BY channels DESC
"""

_SOURCES_SQL = """
SELECT DISTINCT s.name, s.kind
FROM direct_imports di
JOIN sources s ON s.id = di.source_id
WHERE di.status IN ('SUCCESS', 'PARTIAL')
ORDER BY s.name
"""


@dataclass(frozen=True)
class NicheMetrics:
    """Aggregated market statistics for a single niche."""

    niche_id: str
    slug: str
    name: str
    channels_count: int
    subscribers_known: int
    median_subscribers: Decimal | None = None
    p25_subscribers: Decimal | None = None
    p75_subscribers: Decimal | None = None
    total_subscribers: Decimal | None = None
    median_err: Decimal | None = None
    median_views: Decimal | None = None
    median_cpv: Decimal | None = None
    median_campaign_price: Decimal | None = None
    median_views_to_subs: Decimal | None = None
    top_channel_share: Decimal | None = None
    median_growth_rate: Decimal | None = None
    channels_with_history: int = 0

    @property
    def iqr_subscribers(self) -> Decimal | None:
        """Interquartile range — spread of the niche without outlier influence."""
        if self.p25_subscribers is None or self.p75_subscribers is None:
            return None
        return self.p75_subscribers - self.p25_subscribers


@dataclass(frozen=True)
class DatasetSummary:
    """What data the analysis is standing on."""

    channels: int = 0
    channels_without_niche: int = 0
    snapshots: int = 0
    snapshot_dates: int = 0
    first_date: date | None = None
    last_date: date | None = None
    niches: int = 0
    imports: int = 0
    platforms: dict[str, int] = field(default_factory=dict)
    sources: tuple[str, ...] = ()

    @property
    def is_empty(self) -> bool:
        return self.snapshots == 0


class MarketStatisticsRepository:
    """Reads aggregated market statistics."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def niche_metrics(
        self,
        *,
        as_of: date | None = None,
        top_n: int = 3,
        platform: Platform | None = None,
    ) -> list[NicheMetrics]:
        """Aggregate the latest snapshot of every channel, grouped by niche.

        ``platform`` restricts the aggregation to one messenger; ``None`` covers
        all of them.
        """
        params = {"as_of": as_of, "platform": platform.value if platform else None}
        rows = (await self._session.execute(text(_METRICS_SQL), params)).mappings().all()
        concentration = {
            row["niche_id"]: row["top_share"]
            for row in (
                await self._session.execute(
                    text(_CONCENTRATION_SQL), {**params, "top_n": top_n}
                )
            )
            .mappings()
            .all()
        }
        growth = {
            row["niche_id"]: (row["median_growth_rate"], row["channels_with_history"])
            for row in (await self._session.execute(text(_GROWTH_SQL), params))
            .mappings()
            .all()
        }

        metrics: list[NicheMetrics] = []
        for row in rows:
            growth_rate, history_count = growth.get(row["niche_id"], (None, 0))
            metrics.append(
                NicheMetrics(
                    niche_id=str(row["niche_id"]),
                    slug=row["slug"],
                    name=row["name"],
                    channels_count=row["channels_count"],
                    subscribers_known=row["subscribers_known"],
                    median_subscribers=row["median_subscribers"],
                    p25_subscribers=row["p25_subscribers"],
                    p75_subscribers=row["p75_subscribers"],
                    total_subscribers=row["total_subscribers"],
                    median_err=row["median_err"],
                    median_views=row["median_views"],
                    median_cpv=row["median_cpv"],
                    median_campaign_price=row["median_campaign_price"],
                    median_views_to_subs=row["median_views_to_subs"],
                    top_channel_share=concentration.get(row["niche_id"]),
                    median_growth_rate=growth_rate,
                    channels_with_history=history_count,
                )
            )
        return metrics

    async def dataset_summary(self, *, platform: Platform | None = None) -> DatasetSummary:
        """High-level description of the data the analysis is based on."""
        params = {"platform": platform.value if platform else None}
        row = (await self._session.execute(text(_DATASET_SQL), params)).mappings().one()
        platforms = {
            str(item["platform"]): item["channels"]
            for item in (await self._session.execute(text(_PLATFORM_SQL), params)).mappings().all()
        }
        sources = tuple(
            item["name"]
            for item in (await self._session.execute(text(_SOURCES_SQL))).mappings().all()
        )
        return DatasetSummary(
            channels=row["channels"],
            channels_without_niche=row["channels_without_niche"],
            snapshots=row["snapshots"],
            snapshot_dates=row["snapshot_dates"],
            first_date=row["first_date"],
            last_date=row["last_date"],
            niches=row["niches"],
            imports=row["imports"],
            platforms=platforms,
            sources=sources,
        )
