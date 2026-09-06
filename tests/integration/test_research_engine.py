"""Integration tests for the research engine against PostgreSQL."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from channel_factory.core.enums import (
    AiCallStatus,
    AiTaskType,
    ClusterStatus,
    ResearchItemStatus,
    ResearchProviderType,
    RunStatus,
    TrustLevel,
)
from channel_factory.db.models import (
    AiGeneration,
    PipelineRun,
    ResearchCluster,
    ResearchItem,
    ResearchSource,
    TopicScore,
)
from channel_factory.db.repositories.research import ResearchClusterRepository
from channel_factory.providers.llm.client import CostLimitError, LlmClient, MissingApiKeyError
from channel_factory.research.config import load_topic_score_config
from channel_factory.research.providers.base import RawResearchItem, SourceConfig
from channel_factory.research.service import ResearchService

pytestmark = pytest.mark.integration

NOW = datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


class FakeProvider:
    """Returns whatever the test tells it to, and records the calls."""

    provider_type = ResearchProviderType.RSS

    def __init__(self, items: list[RawResearchItem]) -> None:
        self.items = items
        self.calls = 0

    async def fetch(self, source: SourceConfig) -> list[RawResearchItem]:
        self.calls += 1
        return self.items


def item(
    external_id: str,
    title: str,
    *,
    url: str | None = None,
    summary: str = "",
    published_at: datetime | None = None,
    metadata: dict | None = None,
) -> RawResearchItem:
    return RawResearchItem(
        external_id=external_id,
        url=url or f"https://example.com/{external_id}",
        title=title,
        summary=summary or None,
        content_excerpt=summary or None,
        published_at=published_at or NOW,
        raw_metadata=metadata or {},
    )


def source_config(key: str = "fake", trust: TrustLevel = TrustLevel.OFFICIAL) -> SourceConfig:
    return SourceConfig(
        key=key,
        name=f"Fake {key}",
        provider=ResearchProviderType.RSS,
        trust=trust,
        url=f"https://example.com/{key}.xml",
        poll_interval_minutes=60,
        reason="test source",
    )


@pytest.fixture
def score_config():
    from channel_factory.core.config import get_settings

    return load_topic_score_config(get_settings().topic_score_config)


def build_service(database, provider, score_config) -> ResearchService:
    return ResearchService(
        database,
        providers={ResearchProviderType.RSS: provider},
        score_config=score_config,
    )


async def count(database, model) -> int:
    async with database.session() as session:
        return (await session.execute(select(func.count()).select_from(model))).scalar_one()


class TestSourceSync:
    async def test_creates_and_updates_sources(self, database, score_config) -> None:
        service = build_service(database, FakeProvider([]), score_config)

        created, updated = await service.sync_sources([source_config()])
        assert (created, updated) == (1, 0)

        created, updated = await service.sync_sources([source_config()])
        assert (created, updated) == (0, 1)
        assert await count(database, ResearchSource) == 1

    async def test_source_removed_from_config_is_disabled_not_deleted(
        self, database, score_config
    ) -> None:
        """Its items keep their origin, so the source row has to survive."""
        service = build_service(database, FakeProvider([]), score_config)
        await service.sync_sources([source_config("a"), source_config("b")])

        await service.sync_sources([source_config("a")])

        async with database.session() as session:
            rows = (await session.execute(select(ResearchSource))).scalars().all()
        by_key = {row.key: row for row in rows}
        assert len(by_key) == 2
        assert by_key["a"].enabled is True
        assert by_key["b"].enabled is False
        assert "removed from config" in (by_key["b"].last_error or "")


class TestPolling:
    async def test_stores_new_items(self, database, score_config) -> None:
        provider = FakeProvider([item("1", "Introducing Gemini 3.8 Flash")])
        service = build_service(database, provider, score_config)
        await service.sync_sources([source_config()])

        outcome = await service.poll(force=True)

        assert outcome.items_new == 1
        assert outcome.sources_polled == 1
        assert await count(database, ResearchItem) == 1

    async def test_repeat_poll_is_idempotent(self, database, score_config) -> None:
        provider = FakeProvider([item("1", "Introducing Gemini 3.8 Flash")])
        service = build_service(database, provider, score_config)
        await service.sync_sources([source_config()])

        await service.poll(force=True)
        second = await service.poll(force=True)

        assert second.items_new == 0
        assert second.items_unchanged == 1
        assert await count(database, ResearchItem) == 1

    async def test_changed_content_updates_the_same_row(self, database, score_config) -> None:
        """A changelog keeps its URL and changes its text; that is one item."""
        provider = FakeProvider([item("1", "Changelog", summary="v1 shipped")])
        service = build_service(database, provider, score_config)
        await service.sync_sources([source_config()])
        await service.poll(force=True)

        provider.items = [item("1", "Changelog", summary="v2 shipped")]
        outcome = await service.poll(force=True)

        assert outcome.items_updated == 1
        assert await count(database, ResearchItem) == 1
        async with database.session() as session:
            stored = (await session.execute(select(ResearchItem))).scalar_one()
        assert stored.revision == 2
        assert stored.content_changed_at is not None

    async def test_poll_interval_is_respected_without_force(
        self, database, score_config
    ) -> None:
        provider = FakeProvider([item("1", "Something")])
        service = build_service(database, provider, score_config)
        await service.sync_sources([source_config()])

        await service.poll(force=True)
        outcome = await service.poll(force=False)

        assert outcome.sources_skipped == 1
        assert provider.calls == 1

    async def test_failing_source_does_not_stop_the_run(self, database, score_config) -> None:
        class Broken(FakeProvider):
            async def fetch(self, source: SourceConfig):
                raise RuntimeError("feed exploded")

        service = ResearchService(
            database,
            providers={ResearchProviderType.RSS: Broken([])},
            score_config=score_config,
        )
        await service.sync_sources([source_config()])

        outcome = await service.poll(force=True)

        assert outcome.sources_failed == 1
        assert outcome.errors
        async with database.session() as session:
            stored = (await session.execute(select(ResearchSource))).scalar_one()
        assert stored.consecutive_failures == 1
        assert "feed exploded" in (stored.last_error or "")

    async def test_run_is_recorded(self, database, score_config) -> None:
        service = build_service(database, FakeProvider([item("1", "A title here")]), score_config)
        await service.sync_sources([source_config()])

        await service.poll(force=True)

        async with database.session() as session:
            run = (await session.execute(select(PipelineRun))).scalar_one()
        assert run.status is RunStatus.SUCCESS
        assert run.counters["items_new"] == 1
        assert run.duration_ms is not None


class TestClustering:
    async def test_one_event_from_three_sources_becomes_one_cluster(
        self, database, score_config
    ) -> None:
        """The whole point of clustering: 3 reports, 1 event."""
        provider = FakeProvider([])
        service = build_service(database, provider, score_config)
        await service.sync_sources(
            [source_config("a"), source_config("b"), source_config("c")]
        )

        for key, title in (
            ("a", "Introducing Gemini 3.8 Flash"),
            ("b", "Google launches Gemini 3.8 Flash for everyone"),
            ("c", "Gemini 3.8 Flash is now available to developers"),
        ):
            provider.items = [item(f"{key}-1", title, url=f"https://{key}.com/news")]
            await service.poll(only=[key], force=True)

        await service.cluster()

        assert await count(database, ResearchItem) == 3
        assert await count(database, ResearchCluster) == 1
        async with database.session() as session:
            cluster = (await session.execute(select(ResearchCluster))).scalar_one()
        assert cluster.item_count == 3
        assert cluster.vendor == "google"

    async def test_unrelated_items_stay_separate(self, database, score_config) -> None:
        provider = FakeProvider(
            [
                item("1", "Introducing Gemini 3.8 Flash"),
                item("2", "Пять лучших приложений для заметок в 2026 году"),
            ]
        )
        service = build_service(database, provider, score_config)
        await service.sync_sources([source_config()])
        await service.poll(force=True)

        await service.cluster()

        assert await count(database, ResearchCluster) == 2

    async def test_consecutive_releases_are_not_merged(self, database, score_config) -> None:
        """Real n8n releases share their changelog text almost entirely."""
        body = "Bug fixes and performance improvements across the editor. " * 15
        provider = FakeProvider(
            [
                item("1", "stable", summary=body, metadata={"tag_name": "n8n@2.37.6"}),
                item("2", "stable", summary=body, metadata={"tag_name": "n8n@2.37.7"}),
            ]
        )
        service = build_service(database, provider, score_config)
        await service.sync_sources([source_config()])
        await service.poll(force=True)

        await service.cluster()

        assert await count(database, ResearchCluster) == 2

    async def test_most_authoritative_source_becomes_primary(
        self, database, score_config
    ) -> None:
        provider = FakeProvider([])
        service = build_service(database, provider, score_config)
        await service.sync_sources(
            [
                source_config("blog", trust=TrustLevel.REPUTABLE_SECONDARY),
                source_config("vendor", trust=TrustLevel.OFFICIAL),
            ]
        )

        provider.items = [item("b1", "Gemini 3.8 Flash reportedly ships", url="https://b.com/x")]
        await service.poll(only=["blog"], force=True)
        provider.items = [item("v1", "Introducing Gemini 3.8 Flash", url="https://v.com/x")]
        await service.poll(only=["vendor"], force=True)

        await service.cluster()

        async with database.session() as session:
            cluster = (await session.execute(select(ResearchCluster))).scalar_one()
        assert cluster.max_trust is TrustLevel.OFFICIAL
        assert cluster.canonical_title == "Introducing Gemini 3.8 Flash"

    async def test_items_are_marked_clustered(self, database, score_config) -> None:
        provider = FakeProvider([item("1", "Introducing Gemini 3.8 Flash")])
        service = build_service(database, provider, score_config)
        await service.sync_sources([source_config()])
        await service.poll(force=True)

        await service.cluster()

        async with database.session() as session:
            stored = (await session.execute(select(ResearchItem))).scalar_one()
        assert stored.status is ResearchItemStatus.CLUSTERED
        assert stored.simhash is not None


class TestScoring:
    async def _prepare(self, database, score_config, title: str = "Introducing Gemini 3.8 Flash"):
        provider = FakeProvider([item("1", title)])
        service = build_service(database, provider, score_config)
        await service.sync_sources([source_config()])
        await service.poll(force=True)
        await service.cluster()
        return service

    async def test_scores_and_stores_a_versioned_row(self, database, score_config) -> None:
        service = await self._prepare(database, score_config)

        outcome = await service.score()

        assert outcome.clusters_scored == 1
        async with database.session() as session:
            score = (await session.execute(select(TopicScore))).scalar_one()
            cluster = (await session.execute(select(ResearchCluster))).scalar_one()
        assert score.score_version == score_config.score_version
        assert score.components
        assert cluster.topic_score is not None
        assert cluster.needs_rescore is False

    async def test_weak_cluster_is_filtered_not_rejected(self, database, score_config) -> None:
        service = await self._prepare(database, score_config)
        async with database.session() as session:
            cluster = (await session.execute(select(ResearchCluster))).scalar_one()
            cluster.max_trust = TrustLevel.UNKNOWN
            cluster.event_at = NOW - timedelta(days=25)
            cluster.first_seen_at = NOW - timedelta(days=25)
            cluster.needs_rescore = True
            await session.commit()

        await service.score()

        async with database.session() as session:
            cluster = (await session.execute(select(ResearchCluster))).scalar_one()
        assert cluster.status is ClusterStatus.FILTERED
        assert cluster.status is not ClusterStatus.REJECTED

    async def test_new_evidence_makes_a_cluster_rescorable(
        self, database, score_config
    ) -> None:
        """A filtered cluster must be able to come back when a source confirms it."""
        service = await self._prepare(database, score_config)
        await service.score()

        async with database.session() as session:
            repo = ResearchClusterRepository(session)
            assert await repo.needing_score() == []
            cluster = (await session.execute(select(ResearchCluster))).scalar_one()
            cluster.status = ClusterStatus.FILTERED
            cluster.needs_rescore = True
            await session.commit()

        async with database.session() as session:
            pending = await ResearchClusterRepository(session).needing_score()
        assert len(pending) == 1

        second = await service.score()
        assert second.clusters_scored == 1
        async with database.session() as session:
            scores = (await session.execute(select(TopicScore))).scalars().all()
        assert len(scores) == 2, "scoring is append-only"

    async def test_llm_judgement_changes_the_decision(self, database, score_config) -> None:
        service = await self._prepare(database, score_config)

        async def judge(_cluster):
            return {
                "relevance": 0,
                "practical_value": 0,
                "audience_fit": 0,
                "content_potential": 0,
            }

        outcome = await service.score(judge=judge)

        assert outcome.llm_used is True
        assert outcome.unavailable_components == {}
        async with database.session() as session:
            cluster = (await session.execute(select(ResearchCluster))).scalar_one()
        assert cluster.status in {ClusterStatus.FILTERED, ClusterStatus.BACKLOG}

    async def test_without_llm_components_are_reported_unavailable(
        self, database, score_config
    ) -> None:
        service = await self._prepare(database, score_config)

        outcome = await service.score()

        assert outcome.llm_used is False
        assert outcome.unavailable_components["relevance"] == 1

    async def test_expired_cluster_is_not_scored(self, database, score_config) -> None:
        service = await self._prepare(database, score_config)
        async with database.session() as session:
            cluster = (await session.execute(select(ResearchCluster))).scalar_one()
            cluster.last_seen_at = NOW - timedelta(days=400)
            cluster.needs_rescore = True
            await session.commit()

        outcome = await service.score()

        assert outcome.expired == 1
        async with database.session() as session:
            cluster = (await session.execute(select(ResearchCluster))).scalar_one()
        assert cluster.status is ClusterStatus.EXPIRED


class TestCostControl:
    async def test_missing_api_key_is_a_clear_error(self, database) -> None:
        client = LlmClient(
            database,
            api_key=None,
            daily_limit_usd=Decimal("1"),
            monthly_limit_usd=Decimal("1"),
        )
        with pytest.raises(MissingApiKeyError, match="ANTHROPIC_API_KEY"):
            _ = client.client

    async def test_limit_blocks_further_calls(self, database) -> None:
        """A runaway loop must stop at the limit, not drain the balance."""
        client = LlmClient(
            database,
            api_key="test-key",
            daily_limit_usd=Decimal("0.10"),
            monthly_limit_usd=Decimal("100"),
        )
        await client.check_limits()

        async with database.session() as session:
            session.add(
                AiGeneration(
                    model="claude-haiku-4-5",
                    task_type=AiTaskType.CLASSIFICATION,
                    status=AiCallStatus.SUCCESS,
                    estimated_cost_usd=Decimal("0.15"),
                )
            )
            await session.commit()

        with pytest.raises(CostLimitError) as excinfo:
            await client.check_limits()
        assert excinfo.value.scope == "daily"

    async def test_spend_is_summed_from_the_ledger(self, database) -> None:
        client = LlmClient(
            database,
            api_key="test-key",
            daily_limit_usd=Decimal("10"),
            monthly_limit_usd=Decimal("10"),
        )
        async with database.session() as session:
            for amount in ("0.01", "0.02"):
                session.add(
                    AiGeneration(
                        model="claude-haiku-4-5",
                        task_type=AiTaskType.SCORING,
                        status=AiCallStatus.SUCCESS,
                        estimated_cost_usd=Decimal(amount),
                    )
                )
            await session.commit()

        spent = await client.spend(since=datetime.now(UTC) - timedelta(hours=1))
        assert spent == Decimal("0.03")
