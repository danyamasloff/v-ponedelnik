"""Filling the daily posting slots.

The channel runs on a cadence — three posts a day — and the thing that keeps a
cadence is not a queue but a rule that can be re-evaluated at any moment:

    for the slot that is open right now, has a post already gone out?

Everything follows from that. There is no schedule table to drift out of sync
with reality, because reality *is* the ``publications`` table: a slot counts as
filled when a publication exists inside its window. That makes the runner
idempotent — running it once an hour, or five times in a minute, or twice after
a reboot, produces at most one post per slot.

A missed run is survivable rather than silently skipped: a slot stays fillable
for ``window`` minutes after it opens, so an hourly runner still catches a slot
whose exact minute it slept through, and nothing double-posts.

Simulated publications count as filled. A dry run must rehearse the real thing,
including the "already posted today" arithmetic; if it did not, the rehearsal
would post three times and the live run once.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, time, timedelta
from enum import StrEnum
from pathlib import Path

from sqlalchemy import func, select

from channel_factory.content.evergreen import EvergreenTopic, load_topics
from channel_factory.core.enums import ClusterStatus, PublicationStatus, TrustLevel
from channel_factory.core.logging import get_logger
from channel_factory.db.models import Publication, ResearchCluster
from channel_factory.db.repositories.research import ResearchClusterRepository
from channel_factory.db.session import Database
from channel_factory.publishers.service import PublishingService, PublishOutcome

logger = get_logger(__name__)

#: A publication in one of these states occupies its slot.
OCCUPYING_STATUSES = (PublicationStatus.PUBLISHED, PublicationStatus.SIMULATED)


class SchedulerAction(StrEnum):
    """What the runner did this time."""

    BREAKING = "BREAKING"
    NO_SLOT = "NO_SLOT"
    ALREADY_FILLED = "ALREADY_FILLED"
    NO_TOPIC = "NO_TOPIC"
    ATTEMPTED = "ATTEMPTED"


@dataclass(frozen=True)
class Slot:
    """One posting slot on one day, in local time."""

    starts_at: datetime
    ends_at: datetime
    index: int

    @property
    def label(self) -> str:
        return f"{self.starts_at:%Y-%m-%d %H:%M}"


@dataclass(frozen=True)
class BreakingRules:
    """When a topic is allowed to jump the queue.

    Every field here exists to make "breaking" rare. A channel that shouts
    daily is a feed; one that interrupts twice a month is worth a
    notification. Fresh and high-scoring is not enough — the event also has to
    be confirmed, either by several sources or by the vendor publishing it.
    """

    enabled: bool = True
    min_score: float = 90.0
    max_age: timedelta = timedelta(hours=4)
    min_sources: int = 2
    max_per_day: int = 2
    min_gap: timedelta = timedelta(minutes=90)


@dataclass(frozen=True)
class SchedulerOutcome:
    """Result of one run, in terms a report can print."""

    action: SchedulerAction
    slot: Slot | None = None
    detail: str | None = None
    outcome: PublishOutcome | None = None
    cluster_id: uuid.UUID | None = None


def parse_slots(spec: str) -> tuple[time, ...]:
    """Parse ``"09:00,14:00,19:00"`` into times, sorted and deduplicated."""
    times: set[time] = set()
    for chunk in spec.split(","):
        raw = chunk.strip()
        if not raw:
            continue
        try:
            hour, minute = (int(part) for part in raw.split(":", 1))
            times.add(time(hour=hour, minute=minute))
        except (ValueError, TypeError) as exc:
            raise ValueError(f"bad slot time {raw!r}, expected HH:MM") from exc
    if not times:
        raise ValueError("no posting slots configured")
    return tuple(sorted(times))


def parse_own_slots(spec: str) -> frozenset[int]:
    """Parse ``"1"`` or ``"0,2"`` into slot indexes we write ourselves."""
    indexes: set[int] = set()
    for chunk in spec.split(","):
        raw = chunk.strip()
        if not raw:
            continue
        try:
            indexes.add(int(raw))
        except ValueError as exc:
            raise ValueError(f"bad own-slot index {raw!r}, expected a number") from exc
    return frozenset(indexes)


def slots_for_day(day: datetime, slot_times: tuple[time, ...], window: timedelta) -> list[Slot]:
    """Every slot of one calendar day, in the day's own timezone."""
    return [
        Slot(
            starts_at=day.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0),
            ends_at=day.replace(hour=at.hour, minute=at.minute, second=0, microsecond=0) + window,
            index=index,
        )
        for index, at in enumerate(slot_times)
    ]


def open_slot(now: datetime, slot_times: tuple[time, ...], window: timedelta) -> Slot | None:
    """The slot whose window contains ``now``, if any.

    Yesterday's late slot is considered too: a window that crosses midnight
    should not be lost just because the date rolled over.
    """
    candidates = slots_for_day(now - timedelta(days=1), slot_times, window)
    candidates += slots_for_day(now, slot_times, window)
    open_now = [slot for slot in candidates if slot.starts_at <= now < slot.ends_at]
    # The most recent one: if two windows overlap, the newer slot is the one
    # we owe a post for.
    return open_now[-1] if open_now else None


class PostingScheduler:
    """Publishes at most one post per slot, from the best available topic."""

    def __init__(
        self,
        database: Database,
        service: PublishingService,
        *,
        slot_times: tuple[time, ...],
        window: timedelta,
        own_slot_indexes: frozenset[int] = frozenset(),
        topics_path: Path | None = None,
        max_topic_age: timedelta | None = None,
        breaking: BreakingRules | None = None,
    ) -> None:
        self._database = database
        self._service = service
        self._slot_times = slot_times
        self._window = window
        # Which slots of the day belong to our own posts rather than to news.
        # A channel that only reacts to other people's releases has no voice of
        # its own, so this is a deliberate reservation, not a fallback.
        self._own_slot_indexes = own_slot_indexes
        self._topics_path = topics_path
        # Stale news is worse than no news: the reader learns the channel is
        # behind, which is exactly the reputation an automated feed must avoid.
        self._max_topic_age = max_topic_age
        self._breaking = breaking or BreakingRules()

    async def run_once(self, now: datetime | None = None) -> SchedulerOutcome:
        """Publish breaking news if there is any, otherwise fill the open slot."""
        moment = now or datetime.now().astimezone()

        hot = await self.breaking_cluster(moment)
        if hot is not None:
            logger.info("scheduler.publishing.breaking", extra={"cluster": str(hot.id)})
            outcome = await self._service.publish_cluster(hot.id)
            return SchedulerOutcome(
                action=SchedulerAction.BREAKING, outcome=outcome, cluster_id=hot.id
            )

        slot = open_slot(moment, self._slot_times, self._window)
        if slot is None:
            return SchedulerOutcome(
                action=SchedulerAction.NO_SLOT,
                detail=f"вне окна публикации (слоты: {self._slot_spec()})",
            )

        if await self.slot_filled(slot):
            return SchedulerOutcome(
                action=SchedulerAction.ALREADY_FILLED,
                slot=slot,
                detail="в этом слоте уже был пост",
            )

        # An own slot is written by us; a news slot falls back to an own post
        # when research has nothing left, because a silent slot helps nobody.
        if slot.index in self._own_slot_indexes:
            topic = await self.next_topic()
            if topic is not None:
                logger.info(
                    "scheduler.publishing.own",
                    extra={"slot": slot.label, "topic": topic.key},
                )
                outcome = await self._service.publish_topic(topic)
                return SchedulerOutcome(
                    action=SchedulerAction.ATTEMPTED, slot=slot, outcome=outcome
                )

        cluster = await self.next_cluster()
        if cluster is None:
            topic = await self.next_topic()
            if topic is not None:
                logger.info(
                    "scheduler.publishing.own",
                    extra={"slot": slot.label, "topic": topic.key, "reason": "no news"},
                )
                outcome = await self._service.publish_topic(topic)
                return SchedulerOutcome(
                    action=SchedulerAction.ATTEMPTED, slot=slot, outcome=outcome
                )
            return SchedulerOutcome(
                action=SchedulerAction.NO_TOPIC,
                slot=slot,
                detail=(
                    "нет ни отобранных новостей, ни неопубликованных собственных тем: "
                    "пополните config/evergreen_topics.yaml или запустите research-poll"
                ),
            )

        logger.info(
            "scheduler.publishing",
            extra={"slot": slot.label, "cluster": str(cluster.id)},
        )
        outcome = await self._service.publish_cluster(cluster.id)
        return SchedulerOutcome(
            action=SchedulerAction.ATTEMPTED,
            slot=slot,
            outcome=outcome,
            cluster_id=cluster.id,
        )

    async def breaking_cluster(self, now: datetime) -> ResearchCluster | None:
        """A topic big enough to publish right now, or nothing.

        Returns nothing far more often than something — that is the point.
        """
        rules = self._breaking
        if not rules.enabled:
            return None

        published = await self._published_cluster_ids()
        async with self._database.session() as session:
            recent = (
                await session.execute(
                    select(Publication.created_at)
                    .where(Publication.status.in_(OCCUPYING_STATUSES))
                    .order_by(Publication.created_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            day_start = now.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
            today = (
                await session.execute(
                    select(func.count())
                    .select_from(Publication)
                    .where(
                        Publication.status.in_(OCCUPYING_STATUSES),
                        Publication.created_at >= day_start,
                        Publication.is_breaking.is_(True),
                    )
                )
            ).scalar_one()

        if today >= rules.max_per_day:
            return None
        if recent is not None and now.astimezone(UTC) - recent < rules.min_gap:
            # Even real news waits a bit: two posts back to back read as noise.
            return None

        async with self._database.session() as session:
            candidates = await ResearchClusterRepository(session).top(
                statuses=[ClusterStatus.SELECTED], limit=20
            )
        for cluster in candidates:
            if cluster.id in published:
                continue
            if cluster.topic_score is None or float(cluster.topic_score) < rules.min_score:
                continue
            seen_at = cluster.event_at or cluster.first_seen_at
            if seen_at is None or now.astimezone(UTC) - seen_at > rules.max_age:
                continue
            # Confirmation: several sources, or the vendor's own announcement.
            first_hand = cluster.max_trust in (TrustLevel.OFFICIAL, TrustLevel.PRIMARY)
            if cluster.item_count < rules.min_sources and not first_hand:
                continue
            return cluster
        return None

    async def slot_filled(self, slot: Slot) -> bool:
        """Whether a post already occupies this slot."""
        async with self._database.session() as session:
            found = await session.execute(
                select(Publication.id)
                .where(
                    Publication.status.in_(OCCUPYING_STATUSES),
                    Publication.created_at >= slot.starts_at.astimezone(UTC),
                    Publication.created_at < slot.ends_at.astimezone(UTC),
                )
                .limit(1)
            )
        return found.scalar_one_or_none() is not None

    async def _published_cluster_ids(self) -> set[uuid.UUID]:
        """Clusters already used, so nothing is published twice."""
        async with self._database.session() as session:
            used = await session.execute(
                select(Publication.research_cluster_id).where(
                    Publication.status.in_(OCCUPYING_STATUSES),
                    Publication.research_cluster_id.is_not(None),
                )
            )
        return {row for row in used.scalars().all() if row is not None}

    async def next_cluster(self) -> ResearchCluster | None:
        """Freshest high-scoring topic that has not been posted yet."""
        published = await self._published_cluster_ids()
        async with self._database.session() as session:
            clusters = await ResearchClusterRepository(session).top(
                statuses=[ClusterStatus.SELECTED], limit=50
            )
        for cluster in clusters:
            if cluster.id in published:
                continue
            if self._max_topic_age is not None:
                seen_at = cluster.event_at or cluster.first_seen_at
                if seen_at is not None:
                    age = datetime.now(UTC) - seen_at
                    if age > self._max_topic_age:
                        continue
            return cluster
        return None

    @property
    def own_slot_indexes(self) -> frozenset[int]:
        return self._own_slot_indexes

    async def published_topic_keys(self) -> set[str]:
        """Own topics already used, so a report can count what is left."""
        async with self._database.session() as session:
            used = await session.execute(
                select(Publication.topic_key).where(
                    Publication.status.in_(OCCUPYING_STATUSES),
                    Publication.topic_key.is_not(None),
                )
            )
        return {row for row in used.scalars().all() if row}

    async def next_topic(self) -> EvergreenTopic | None:
        """First own topic that has not been published yet."""
        if self._topics_path is None:
            return None
        try:
            topics = load_topics(self._topics_path)
        except Exception as exc:
            logger.warning("scheduler.topics_unreadable", extra={"error": str(exc)})
            return None

        published = await self.published_topic_keys()
        for topic in topics:
            if topic.key not in published:
                return topic
        return None

    async def upcoming(self, days: int = 1, now: datetime | None = None) -> list[Slot]:
        """Slots from now until ``days`` ahead — what the plan looks like."""
        moment = now or datetime.now().astimezone()
        plan: list[Slot] = []
        for offset in range(days):
            for slot in slots_for_day(
                moment + timedelta(days=offset), self._slot_times, self._window
            ):
                if slot.ends_at > moment:
                    plan.append(slot)
        return plan

    def _slot_spec(self) -> str:
        return ", ".join(f"{at:%H:%M}" for at in self._slot_times)
