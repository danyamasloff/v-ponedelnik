"""Competitor analysis over imported market data.

Scope, stated plainly: the catalog export describes how channels are *offered
to advertisers* — audience size, engagement, price. It contains no posts, no
posting frequency and no headlines, so this module answers "who leads this
niche and what does it take to compete" and cannot answer "what content works".
Content-pattern analysis needs a different data source and is not faked here.

Positions are computed within a channel's own niche **and platform**: a Telegram
channel is a peer of other Telegram channels, not of MAX ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from channel_factory.core.enums import Platform


class LeaderMetric(StrEnum):
    """What "leading" means for a query."""

    SUBSCRIBERS = "subscribers"
    VIEWS = "predicted_views"
    ENGAGEMENT = "err"
    PRICE = "campaign_price"
    CPV = "cpv"


# Ordering column per metric, whitelisted: the value is interpolated into SQL,
# so it must never come straight from user input.
_ORDER_COLUMNS = {
    LeaderMetric.SUBSCRIBERS: "subscribers",
    LeaderMetric.VIEWS: "predicted_views",
    LeaderMetric.ENGAGEMENT: "err",
    LeaderMetric.PRICE: "campaign_price",
    LeaderMetric.CPV: "cpv",
}

_PLATFORM_FILTER = (
    "(CAST(:platform AS text) IS NULL OR mc.platform::text = CAST(:platform AS text))"
)

_LATEST = """
    latest AS (
        SELECT DISTINCT ON (ms.market_channel_id)
            ms.market_channel_id,
            ms.snapshot_date,
            ms.subscribers,
            ms.err,
            ms.predicted_views,
            ms.cpv,
            ms.campaign_price,
            ms.currency
        FROM market_snapshots ms
        ORDER BY ms.market_channel_id, ms.snapshot_date DESC
    )
"""

# Every channel with its standing inside its own niche and platform.
_ENRICHED = """
    enriched AS (
        SELECT
            mc.id,
            mc.channel_name,
            mc.channel_url,
            mc.platform::text AS platform,
            n.slug  AS niche_slug,
            n.name  AS niche_name,
            l.snapshot_date,
            l.subscribers,
            l.err,
            l.predicted_views,
            l.cpv,
            l.campaign_price,
            l.currency,
            cume_dist() OVER (
                PARTITION BY mc.niche_id, mc.platform ORDER BY l.subscribers
            ) * 100 AS subscribers_pct,
            cume_dist() OVER (
                PARTITION BY mc.niche_id, mc.platform ORDER BY l.err
            ) * 100 AS err_pct,
            cume_dist() OVER (
                PARTITION BY mc.niche_id, mc.platform ORDER BY l.predicted_views
            ) * 100 AS views_pct
        FROM latest l
        JOIN market_channels mc ON mc.id = l.market_channel_id
        LEFT JOIN niches n ON n.id = mc.niche_id
    )
"""

_BENCHMARKS_SQL = f"""
WITH {_LATEST},
scoped AS (
    SELECT l.*
    FROM latest l
    JOIN market_channels mc ON mc.id = l.market_channel_id
    JOIN niches n ON n.id = mc.niche_id
    WHERE n.slug = :slug AND {_PLATFORM_FILTER}
)
SELECT
    count(*)                                                        AS channels_count,
    percentile_cont(0.10) WITHIN GROUP (ORDER BY subscribers)       AS subscribers_p10,
    percentile_cont(0.25) WITHIN GROUP (ORDER BY subscribers)       AS subscribers_p25,
    percentile_cont(0.50) WITHIN GROUP (ORDER BY subscribers)       AS subscribers_p50,
    percentile_cont(0.75) WITHIN GROUP (ORDER BY subscribers)       AS subscribers_p75,
    percentile_cont(0.90) WITHIN GROUP (ORDER BY subscribers)       AS subscribers_p90,
    max(subscribers)                                                AS subscribers_max,
    sum(subscribers)                                                AS subscribers_total,
    percentile_cont(0.25) WITHIN GROUP (ORDER BY err)               AS err_p25,
    percentile_cont(0.50) WITHIN GROUP (ORDER BY err)               AS err_p50,
    percentile_cont(0.75) WITHIN GROUP (ORDER BY err)               AS err_p75,
    percentile_cont(0.50) WITHIN GROUP (ORDER BY predicted_views)   AS views_p50,
    percentile_cont(0.90) WITHIN GROUP (ORDER BY predicted_views)   AS views_p90,
    percentile_cont(0.50) WITHIN GROUP (ORDER BY cpv)               AS cpv_p50,
    percentile_cont(0.50) WITHIN GROUP (ORDER BY campaign_price)    AS price_p50,
    max(snapshot_date)                                              AS snapshot_date
FROM scoped
"""

# Share of the niche's subscribers held by its largest channels. Reported for a
# fixed head size so it is comparable between niches of different sizes.
_HEAD_SHARE_SQL = f"""
WITH {_LATEST},
ranked AS (
    SELECT
        l.subscribers,
        row_number() OVER (ORDER BY l.subscribers DESC NULLS LAST) AS position
    FROM latest l
    JOIN market_channels mc ON mc.id = l.market_channel_id
    JOIN niches n ON n.id = mc.niche_id
    WHERE n.slug = :slug AND {_PLATFORM_FILTER} AND l.subscribers IS NOT NULL
)
SELECT
    sum(subscribers) FILTER (WHERE position <= :head)::numeric
        / NULLIF(sum(subscribers), 0) AS head_share,
    count(*)                          AS ranked_channels
FROM ranked
"""

_CHANNEL_BY_ID_SQL = f"""
WITH {_LATEST},
{_ENRICHED}
SELECT * FROM enriched WHERE id = CAST(:channel_id AS uuid)
"""

_SEARCH_SQL = f"""
WITH {_LATEST},
{_ENRICHED}
SELECT * FROM enriched
WHERE channel_name ILIKE :pattern OR channel_url ILIKE :pattern
ORDER BY subscribers DESC NULLS LAST
LIMIT :limit
"""

_WATCHLIST_SQL = f"""
WITH {_LATEST},
{_ENRICHED}
SELECT e.*, w.notes AS watch_notes, w.created_at AS watched_at
FROM competitor_watchlist w
JOIN enriched e ON e.id = w.market_channel_id
ORDER BY e.niche_name NULLS LAST, e.subscribers DESC NULLS LAST
"""


@dataclass(frozen=True)
class ChannelProfile:
    """One channel with its standing inside its niche and platform."""

    channel_id: str
    channel_name: str
    channel_url: str | None
    platform: str
    niche_slug: str | None
    niche_name: str | None
    snapshot_date: date | None
    subscribers: int | None = None
    err: Decimal | None = None
    predicted_views: int | None = None
    cpv: Decimal | None = None
    campaign_price: Decimal | None = None
    currency: str | None = None
    subscribers_pct: float | None = None
    err_pct: float | None = None
    views_pct: float | None = None
    watch_notes: str | None = None

    @property
    def views_to_subscribers(self) -> float | None:
        if not self.subscribers or self.predicted_views is None:
            return None
        return self.predicted_views / self.subscribers


@dataclass(frozen=True)
class NicheBenchmarks:
    """What it takes to compete in a niche."""

    slug: str
    name: str
    platform: Platform | None
    channels_count: int
    snapshot_date: date | None = None
    subscribers_p10: Decimal | None = None
    subscribers_p25: Decimal | None = None
    subscribers_p50: Decimal | None = None
    subscribers_p75: Decimal | None = None
    subscribers_p90: Decimal | None = None
    subscribers_max: int | None = None
    subscribers_total: int | None = None
    err_p25: Decimal | None = None
    err_p50: Decimal | None = None
    err_p75: Decimal | None = None
    views_p50: Decimal | None = None
    views_p90: Decimal | None = None
    cpv_p50: Decimal | None = None
    price_p50: Decimal | None = None
    head_share: Decimal | None = None
    head_size: int = 10


def _profile_from_row(row) -> ChannelProfile:
    return ChannelProfile(
        channel_id=str(row["id"]),
        channel_name=row["channel_name"],
        channel_url=row["channel_url"],
        platform=row["platform"],
        niche_slug=row["niche_slug"],
        niche_name=row["niche_name"],
        snapshot_date=row["snapshot_date"],
        subscribers=row["subscribers"],
        err=row["err"],
        predicted_views=row["predicted_views"],
        cpv=row["cpv"],
        campaign_price=row["campaign_price"],
        currency=row["currency"],
        subscribers_pct=row["subscribers_pct"],
        err_pct=row["err_pct"],
        views_pct=row["views_pct"],
        watch_notes=row.get("watch_notes"),
    )


class CompetitorRepository:
    """Reads competitor-facing views of the market data."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def niche_benchmarks(
        self,
        slug: str,
        *,
        platform: Platform | None = None,
        head_size: int = 10,
    ) -> NicheBenchmarks | None:
        """Distribution of a niche, or ``None`` when the niche has no channels."""
        params = {"slug": slug, "platform": platform.value if platform else None}
        row = (await self._session.execute(text(_BENCHMARKS_SQL), params)).mappings().one_or_none()
        if row is None or not row["channels_count"]:
            return None

        name = (
            await self._session.execute(
                text("SELECT name FROM niches WHERE slug = :slug"), {"slug": slug}
            )
        ).scalar_one_or_none()

        head = (
            (
                await self._session.execute(
                    text(_HEAD_SHARE_SQL), {**params, "head": head_size}
                )
            )
            .mappings()
            .one_or_none()
        )

        return NicheBenchmarks(
            slug=slug,
            name=name or slug,
            platform=platform,
            channels_count=row["channels_count"],
            snapshot_date=row["snapshot_date"],
            subscribers_p10=row["subscribers_p10"],
            subscribers_p25=row["subscribers_p25"],
            subscribers_p50=row["subscribers_p50"],
            subscribers_p75=row["subscribers_p75"],
            subscribers_p90=row["subscribers_p90"],
            subscribers_max=row["subscribers_max"],
            subscribers_total=row["subscribers_total"],
            err_p25=row["err_p25"],
            err_p50=row["err_p50"],
            err_p75=row["err_p75"],
            views_p50=row["views_p50"],
            views_p90=row["views_p90"],
            cpv_p50=row["cpv_p50"],
            price_p50=row["price_p50"],
            head_share=head["head_share"] if head else None,
            head_size=head_size,
        )

    async def niche_leaders(
        self,
        slug: str,
        *,
        platform: Platform | None = None,
        metric: LeaderMetric = LeaderMetric.SUBSCRIBERS,
        limit: int = 10,
        min_subscribers: int | None = None,
    ) -> list[ChannelProfile]:
        """Top channels of a niche by one metric.

        ``min_subscribers`` matters for engagement rankings: without a floor the
        list fills up with tiny channels whose ERR is an artefact of a small
        denominator rather than a sign of a strong channel.
        """
        column = _ORDER_COLUMNS[metric]
        query = f"""
        WITH {_LATEST},
        {_ENRICHED}
        SELECT * FROM enriched
        WHERE niche_slug = :slug
          AND (CAST(:platform AS text) IS NULL OR platform = CAST(:platform AS text))
          AND (
              CAST(:min_subscribers AS bigint) IS NULL
              OR subscribers >= CAST(:min_subscribers AS bigint)
          )
          AND {column} IS NOT NULL
        ORDER BY {column} DESC
        LIMIT :limit
        """
        rows = (
            await self._session.execute(
                text(query),
                {
                    "slug": slug,
                    "platform": platform.value if platform else None,
                    "limit": limit,
                    "min_subscribers": min_subscribers,
                },
            )
        ).mappings()
        return [_profile_from_row(row) for row in rows]

    async def channel_profile(self, channel_id: str) -> ChannelProfile | None:
        row = (
            (await self._session.execute(text(_CHANNEL_BY_ID_SQL), {"channel_id": channel_id}))
            .mappings()
            .one_or_none()
        )
        return None if row is None else _profile_from_row(row)

    async def search_channels(self, query: str, *, limit: int = 20) -> list[ChannelProfile]:
        rows = (
            await self._session.execute(
                text(_SEARCH_SQL), {"pattern": f"%{query}%", "limit": limit}
            )
        ).mappings()
        return [_profile_from_row(row) for row in rows]

    async def watchlist(self) -> list[ChannelProfile]:
        rows = (await self._session.execute(text(_WATCHLIST_SQL))).mappings()
        return [_profile_from_row(row) for row in rows]
