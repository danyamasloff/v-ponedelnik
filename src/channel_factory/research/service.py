"""Research engine orchestration: poll -> cluster -> score.

Each stage is a separate, restartable job that records a ``pipeline_runs`` row,
so an autonomous system can be asked afterwards what it did and why.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from channel_factory.core.enums import (
    ClusterItemRole,
    ClusterStatus,
    EventType,
    MatchMethod,
    PipelineJob,
    ResearchItemStatus,
    ResearchProviderType,
    RunStatus,
    TrustLevel,
)
from channel_factory.core.logging import get_logger
from channel_factory.db.models import (
    PipelineRun,
    ResearchCluster,
    ResearchClusterItem,
    ResearchItem,
    ResearchSource,
    TopicScore,
)
from channel_factory.db.repositories.research import (
    ResearchClusterRepository,
    ResearchItemRepository,
    ResearchSourceRepository,
)
from channel_factory.db.session import Database
from channel_factory.research.canonical import (
    canonicalize_url,
    content_fingerprint,
    from_signed_64,
    to_signed_64,
    url_hash,
)
from channel_factory.research.config import TopicScoreConfig
from channel_factory.research.dedup import (
    DEFAULT_TIME_WINDOW_HOURS,
    ClusterMatch,
    ItemSignature,
    build_signature,
    entity_cluster_key,
    fallback_cluster_key,
    match_cluster,
)
from channel_factory.research.lexicon import LexiconVerdict, TopicLexicon
from channel_factory.research.providers.base import (
    ProviderError,
    RawResearchItem,
    SourceConfig,
)
from channel_factory.research.scoring import ClusterScoreInput, score_cluster

logger = get_logger(__name__)

CANDIDATE_WINDOW_DAYS = 7
TRUST_ORDER = {
    TrustLevel.UNKNOWN: 0,
    TrustLevel.AGGREGATOR: 1,
    TrustLevel.REPUTABLE_SECONDARY: 2,
    TrustLevel.PRIMARY: 3,
    TrustLevel.OFFICIAL: 4,
}


@dataclass
class PollOutcome:
    """What one poll run did."""

    run_id: uuid.UUID | None = None
    sources_polled: int = 0
    sources_skipped: int = 0
    sources_failed: int = 0
    items_new: int = 0
    items_updated: int = 0
    items_unchanged: int = 0
    duration_ms: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class ClusterOutcome:
    """What one clustering run did."""

    run_id: uuid.UUID | None = None
    items_processed: int = 0
    clusters_created: int = 0
    items_attached: int = 0
    by_method: dict[str, int] = field(default_factory=dict)
    duration_ms: int = 0


@dataclass
class ScoreOutcome:
    """What one scoring run did."""

    run_id: uuid.UUID | None = None
    clusters_scored: int = 0
    selected: int = 0
    backlog: int = 0
    filtered: int = 0
    expired: int = 0
    llm_used: bool = False
    lexicon_used: bool = False
    unavailable_components: dict[str, int] = field(default_factory=dict)
    duration_ms: int = 0


class ResearchService:
    """Runs the research pipeline stages."""

    def __init__(
        self,
        database: Database,
        *,
        providers: dict[ResearchProviderType, Any],
        score_config: TopicScoreConfig,
        lexicon: TopicLexicon | None = None,
    ) -> None:
        self._database = database
        self._providers = providers
        self._score_config = score_config
        self._lexicon = lexicon

    # ---------------------------------------------------------------- sources

    async def sync_sources(self, configs: list[SourceConfig]) -> tuple[int, int]:
        """Bring the ``research_sources`` table in line with the whitelist.

        Sources are never deleted here: a source removed from the config is
        disabled instead, so the items it already produced keep their origin.
        """
        created = updated = 0
        async with self._database.session() as session:
            repo = ResearchSourceRepository(session)
            configured_keys = {config.key for config in configs}

            for config in configs:
                source = await repo.by_key(config.key)
                if source is None:
                    session.add(
                        ResearchSource(
                            key=config.key,
                            name=config.name,
                            provider=config.provider,
                            trust=config.trust,
                            url=config.url,
                            repo=config.repo,
                            enabled=config.enabled,
                            poll_interval_minutes=config.poll_interval_minutes,
                            reason=config.reason,
                            config=config.config,
                        )
                    )
                    created += 1
                else:
                    source.name = config.name
                    source.provider = config.provider
                    source.trust = config.trust
                    source.url = config.url
                    source.repo = config.repo
                    source.enabled = config.enabled
                    source.poll_interval_minutes = config.poll_interval_minutes
                    source.reason = config.reason
                    source.config = config.config
                    updated += 1

            for source in await repo.all():
                if source.key not in configured_keys and source.enabled:
                    source.enabled = False
                    source.last_error = "removed from config/research_sources.yaml"
            await session.commit()
        return created, updated

    # ------------------------------------------------------------------- poll

    async def poll(
        self, *, only: list[str] | None = None, force: bool = False, limit_per_source: int = 100
    ) -> PollOutcome:
        """Fetch every due source and store what is new or changed."""
        started = time.perf_counter()
        outcome = PollOutcome()
        run_id = await self._start_run(PipelineJob.RESEARCH_POLL)
        outcome.run_id = run_id
        now = datetime.now(UTC)

        async with self._database.session() as session:
            sources = await ResearchSourceRepository(session).all(enabled_only=True)

        for source in sources:
            if only and source.key not in only:
                continue
            if not force and not self._is_due(source, now):
                outcome.sources_skipped += 1
                continue

            provider = self._providers.get(source.provider)
            if provider is None:
                outcome.sources_failed += 1
                outcome.errors.append(f"{source.key}: no provider for {source.provider}")
                continue

            config = SourceConfig(
                key=source.key,
                name=source.name,
                provider=source.provider,
                trust=source.trust,
                url=source.url,
                repo=source.repo,
                poll_interval_minutes=source.poll_interval_minutes,
                config=source.config or {},
            )

            try:
                raw_items = await provider.fetch(config)
            except ProviderError as exc:
                outcome.sources_failed += 1
                outcome.errors.append(f"{source.key}: {exc}")
                await self._record_source_failure(source.id, str(exc))
                logger.warning(
                    "source poll failed", extra={"source": source.key, "reason": str(exc)}
                )
                continue
            except Exception as exc:
                outcome.sources_failed += 1
                outcome.errors.append(f"{source.key}: {type(exc).__name__}: {exc}")
                await self._record_source_failure(source.id, f"{type(exc).__name__}: {exc}")
                logger.exception("source poll crashed", extra={"source": source.key})
                continue

            stats = await self._store_items(source, raw_items[:limit_per_source])
            outcome.items_new += stats[0]
            outcome.items_updated += stats[1]
            outcome.items_unchanged += stats[2]
            outcome.sources_polled += 1
            await self._record_source_success(source.id)

        outcome.duration_ms = int((time.perf_counter() - started) * 1000)
        await self._finish_run(
            run_id,
            status=RunStatus.PARTIAL if outcome.sources_failed else RunStatus.SUCCESS,
            counters={
                "sources_polled": outcome.sources_polled,
                "sources_skipped": outcome.sources_skipped,
                "sources_failed": outcome.sources_failed,
                "items_new": outcome.items_new,
                "items_updated": outcome.items_updated,
                "items_unchanged": outcome.items_unchanged,
            },
            duration_ms=outcome.duration_ms,
            error="; ".join(outcome.errors[:5]) if outcome.errors else None,
        )
        logger.info(
            "research poll finished",
            extra={
                "sources": outcome.sources_polled,
                "new": outcome.items_new,
                "updated": outcome.items_updated,
                "failed": outcome.sources_failed,
                "duration_ms": outcome.duration_ms,
            },
        )
        return outcome

    @staticmethod
    def _is_due(source: ResearchSource, now: datetime) -> bool:
        if source.last_polled_at is None:
            return True
        due_at = source.last_polled_at + timedelta(minutes=source.poll_interval_minutes)
        return now >= due_at

    async def _store_items(
        self, source: ResearchSource, raw_items: list[RawResearchItem]
    ) -> tuple[int, int, int]:
        """Insert new items, update changed ones, leave unchanged ones alone."""
        new = updated = unchanged = 0
        now = datetime.now(UTC)

        async with self._database.session() as session:
            repo = ResearchItemRepository(session)
            cluster_repo = ResearchClusterRepository(session)

            for raw in raw_items:
                try:
                    canonical = canonicalize_url(raw.url)
                except ValueError:
                    continue
                fingerprint = content_fingerprint(
                    raw.title, raw.summary, raw.content_excerpt
                )
                existing = await repo.by_natural_key(source.id, raw.external_id)

                if existing is None:
                    session.add(
                        ResearchItem(
                            research_source_id=source.id,
                            source_type=source.provider,
                            trust=source.trust,
                            external_id=raw.external_id[:512],
                            url=raw.url[:2048],
                            canonical_url=canonical[:2048],
                            url_hash=url_hash(canonical),
                            title=raw.title[:1024],
                            summary=raw.summary,
                            content_excerpt=raw.content_excerpt,
                            content_fingerprint=fingerprint,
                            published_at=raw.published_at,
                            discovered_at=now,
                            author=raw.author,
                            publisher=raw.publisher,
                            language=raw.language,
                            raw_metadata=raw.raw_metadata,
                            status=ResearchItemStatus.NEW,
                        )
                    )
                    new += 1
                elif existing.content_fingerprint != fingerprint:
                    # Same address, new content: keep one identity and record
                    # that it moved, rather than creating a second row.
                    existing.title = raw.title[:1024]
                    existing.summary = raw.summary
                    existing.content_excerpt = raw.content_excerpt
                    existing.content_fingerprint = fingerprint
                    existing.published_at = raw.published_at or existing.published_at
                    existing.content_changed_at = now
                    existing.revision += 1
                    existing.raw_metadata = raw.raw_metadata
                    cluster = await cluster_repo.cluster_of_item(existing.id)
                    if cluster is not None:
                        cluster.needs_rescore = True
                    updated += 1
                else:
                    unchanged += 1

            await session.commit()
        return new, updated, unchanged

    async def _record_source_success(self, source_id: uuid.UUID) -> None:
        async with self._database.session() as session:
            source = await session.get(ResearchSource, source_id)
            if source is not None:
                now = datetime.now(UTC)
                source.last_polled_at = now
                source.last_success_at = now
                source.consecutive_failures = 0
                source.last_error = None
            await session.commit()

    async def _record_source_failure(self, source_id: uuid.UUID, message: str) -> None:
        async with self._database.session() as session:
            source = await session.get(ResearchSource, source_id)
            if source is not None:
                source.last_polled_at = datetime.now(UTC)
                source.consecutive_failures += 1
                source.last_error = message[:2000]
            await session.commit()

    # ---------------------------------------------------------------- cluster

    async def cluster(self, *, limit: int = 500) -> ClusterOutcome:
        """Attach new items to existing events, or open new ones."""
        started = time.perf_counter()
        outcome = ClusterOutcome()
        run_id = await self._start_run(PipelineJob.RESEARCH_CLUSTER)
        outcome.run_id = run_id
        since = datetime.now(UTC) - timedelta(days=CANDIDATE_WINDOW_DAYS)

        async with self._database.session() as session:
            item_repo = ResearchItemRepository(session)
            cluster_repo = ResearchClusterRepository(session)

            pending = await item_repo.unclustered(limit=limit)
            candidates_raw = await item_repo.clustered_since(since)
            candidates: list[ItemSignature] = [
                self._signature(item) for item in candidates_raw
            ]
            # item_id -> cluster, so a match on an item resolves to its cluster.
            cluster_by_item: dict[str, ResearchCluster] = {}
            for item in candidates_raw:
                cluster = await cluster_repo.cluster_of_item(item.id)
                if cluster is not None:
                    cluster_by_item[str(item.id)] = cluster

            for item in pending:
                signature = self._signature(item)
                outcome.items_processed += 1

                matched = match_cluster(signature, candidates)
                cluster: ResearchCluster | None = None
                method = MatchMethod.SEED
                similarity: float | None = None

                if matched is not None:
                    other, method, similarity = matched
                    cluster = cluster_by_item.get(other.item_id)

                if cluster is None:
                    key = entity_cluster_key(signature) or fallback_cluster_key(signature)
                    cluster = await cluster_repo.by_key(key)
                    if cluster is None:
                        cluster = ResearchCluster(
                            cluster_key=key[:512],
                            event_type=signature.event_type,
                            canonical_title=item.title[:1024],
                            canonical_summary=item.summary,
                            primary_item_id=item.id,
                            vendor=signature.vendor,
                            product=signature.product,
                            version=signature.version,
                            event_at=item.published_at or item.discovered_at,
                            first_seen_at=item.discovered_at,
                            last_seen_at=item.discovered_at,
                            item_count=0,
                            max_trust=item.trust,
                            status=ClusterStatus.OPEN,
                            needs_rescore=True,
                        )
                        session.add(cluster)
                        await session.flush()
                        outcome.clusters_created += 1
                        method = MatchMethod.SEED

                role = (
                    ClusterItemRole.PRIMARY
                    if cluster.primary_item_id == item.id
                    else ClusterItemRole.SUPPORTING
                )
                session.add(
                    ResearchClusterItem(
                        research_cluster_id=cluster.id,
                        research_item_id=item.id,
                        role=role,
                        matched_by=method,
                        similarity=similarity,
                    )
                )
                item.status = ResearchItemStatus.CLUSTERED
                item.simhash = to_signed_64(signature.simhash)

                cluster.item_count += 1
                cluster.last_seen_at = max(cluster.last_seen_at, item.discovered_at)
                cluster.needs_rescore = True
                if TRUST_ORDER[item.trust] > TRUST_ORDER[cluster.max_trust]:
                    # The most authoritative report becomes the primary one.
                    cluster.max_trust = item.trust
                    cluster.primary_item_id = item.id
                    cluster.canonical_title = item.title[:1024]

                outcome.items_attached += 1
                outcome.by_method[method.value] = outcome.by_method.get(method.value, 0) + 1
                candidates.append(signature)
                cluster_by_item[str(item.id)] = cluster

            await session.commit()

        outcome.duration_ms = int((time.perf_counter() - started) * 1000)
        await self._finish_run(
            run_id,
            status=RunStatus.SUCCESS,
            counters={
                "items_processed": outcome.items_processed,
                "clusters_created": outcome.clusters_created,
                "items_attached": outcome.items_attached,
                "by_method": outcome.by_method,
            },
            duration_ms=outcome.duration_ms,
        )
        logger.info(
            "research clustering finished",
            extra={
                "processed": outcome.items_processed,
                # Not "created": LogRecord already owns that attribute, and
                # logging raises KeyError rather than overwriting it.
                "clusters_created": outcome.clusters_created,
                "duration_ms": outcome.duration_ms,
            },
        )
        return outcome

    @staticmethod
    def _signature(item: ResearchItem) -> ItemSignature:
        metadata = item.raw_metadata or {}
        stored = build_signature(
            item_id=str(item.id),
            url_hash=item.url_hash,
            content_fingerprint=item.content_fingerprint,
            title=item.title,
            summary=item.summary,
            event_at=item.published_at or item.discovered_at,
            # Providers that know their own identifiers say so here; the title
            # of a release is often just "stable".
            version_hint=metadata.get("tag_name"),
            product_hint=metadata.get("repo"),
        )
        if item.simhash is not None:
            # Reuse the stored value so a signature never drifts from what was
            # persisted when the item was first clustered.
            return ItemSignature(**{**stored.__dict__, "simhash": from_signed_64(item.simhash)})
        return stored

    # ------------------------------------------------------------------ score

    async def score(self, *, limit: int = 200, judge: Any = None) -> ScoreOutcome:
        """Score clusters that are new or have gained evidence."""
        started = time.perf_counter()
        outcome = ScoreOutcome()
        run_id = await self._start_run(PipelineJob.TOPIC_SCORE)
        outcome.run_id = run_id
        now = datetime.now(UTC)
        expiry = now - timedelta(days=self._score_config.expire_after_days)

        async with self._database.session() as session:
            repo = ResearchClusterRepository(session)
            clusters = await repo.needing_score(limit=limit)

            for cluster in clusters:
                if cluster.last_seen_at < expiry:
                    cluster.status = ClusterStatus.EXPIRED
                    cluster.needs_rescore = False
                    outcome.expired += 1
                    continue

                judgement = await judge(cluster) if judge is not None else None
                result = score_cluster(
                    ClusterScoreInput(
                        cluster_key=cluster.cluster_key,
                        event_type=cluster.event_type or EventType.OTHER,
                        max_trust=cluster.max_trust,
                        event_at=cluster.event_at,
                        first_seen_at=cluster.first_seen_at,
                        item_count=cluster.item_count,
                        llm_judgement=judgement,
                        lexicon=self._lexicon_verdicts(cluster),
                    ),
                    self._score_config,
                    now=now,
                )

                session.add(
                    TopicScore(
                        research_cluster_id=cluster.id,
                        score_version=self._score_config.score_version,
                        score=result.score,
                        decision=result.decision,
                        components=result.components_dict(),
                        inputs=result.inputs,
                        item_count=cluster.item_count,
                        as_of_date=now.date(),
                    )
                )
                cluster.topic_score = result.score
                cluster.score_version = self._score_config.score_version
                cluster.scored_at = now
                cluster.status = result.decision
                cluster.needs_rescore = False

                outcome.clusters_scored += 1
                outcome.llm_used = outcome.llm_used or result.used_llm
                outcome.lexicon_used = outcome.lexicon_used or result.used_lexicon
                for key in result.unavailable:
                    outcome.unavailable_components[key] = (
                        outcome.unavailable_components.get(key, 0) + 1
                    )
                if result.decision is ClusterStatus.SELECTED:
                    outcome.selected += 1
                elif result.decision is ClusterStatus.BACKLOG:
                    outcome.backlog += 1
                elif result.decision is ClusterStatus.FILTERED:
                    outcome.filtered += 1

            await session.commit()

        outcome.duration_ms = int((time.perf_counter() - started) * 1000)
        await self._finish_run(
            run_id,
            status=RunStatus.SUCCESS,
            counters={
                "clusters_scored": outcome.clusters_scored,
                "selected": outcome.selected,
                "backlog": outcome.backlog,
                "filtered": outcome.filtered,
                "expired": outcome.expired,
                "llm_used": outcome.llm_used,
                "unavailable_components": outcome.unavailable_components,
            },
            duration_ms=outcome.duration_ms,
        )
        logger.info(
            "topic scoring finished",
            extra={
                "scored": outcome.clusters_scored,
                "selected": outcome.selected,
                "llm_used": outcome.llm_used,
                "duration_ms": outcome.duration_ms,
            },
        )
        return outcome

    def _lexicon_verdicts(self, cluster: ResearchCluster) -> dict[str, LexiconVerdict] | None:
        """Deterministic fallbacks for relevance and practical value."""
        if self._lexicon is None:
            return None
        text = f"{cluster.canonical_title} {cluster.canonical_summary or ''}"
        return {
            "relevance": self._lexicon.relevance(text),
            "practical_value": self._lexicon.practical_value(
                cluster.canonical_title,
                cluster.canonical_summary,
                cluster.event_type or EventType.OTHER,
                has_version=bool(cluster.version),
            ),
        }

    # -------------------------------------------------------------- run audit

    async def _start_run(self, job: PipelineJob) -> uuid.UUID:
        async with self._database.session() as session:
            run = PipelineRun(
                job=job, status=RunStatus.RUNNING, started_at=datetime.now(UTC)
            )
            session.add(run)
            await session.commit()
            return run.id

    async def _finish_run(
        self,
        run_id: uuid.UUID,
        *,
        status: RunStatus,
        counters: dict[str, Any],
        duration_ms: int,
        error: str | None = None,
    ) -> None:
        async with self._database.session() as session:
            run = await session.get(PipelineRun, run_id)
            if run is not None:
                run.status = status
                run.finished_at = datetime.now(UTC)
                run.duration_ms = duration_ms
                run.counters = counters
                run.error_message = error[:2000] if error else None
            await session.commit()


__all__ = [
    "DEFAULT_TIME_WINDOW_HOURS",
    "ClusterMatch",
    "ClusterOutcome",
    "PollOutcome",
    "ResearchService",
    "ScoreOutcome",
]
