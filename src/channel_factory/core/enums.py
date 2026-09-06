"""Domain enums shared across modules.

All enum members use identical name and value so that the value stored in
PostgreSQL is stable and human-readable in raw SQL queries.
"""

from __future__ import annotations

from enum import StrEnum


class Platform(StrEnum):
    """Messenger platform a channel belongs to."""

    TELEGRAM = "TELEGRAM"
    MAX = "MAX"
    UNKNOWN = "UNKNOWN"


class SourceKind(StrEnum):
    """Where a batch of data originally came from."""

    DIRECT_EXPORT = "DIRECT_EXPORT"
    CATALOG = "CATALOG"
    CONTENT_REFERENCE = "CONTENT_REFERENCE"
    OTHER = "OTHER"


class ImportStatus(StrEnum):
    """Lifecycle status of a single import run.

    RUNNING is written before processing starts so a crashed import stays
    visible in the history instead of disappearing with the transaction.
    """

    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"


class RowStatus(StrEnum):
    """Per-row outcome of the import pipeline."""

    VALID = "VALID"
    REJECTED = "REJECTED"


class ResearchProviderType(StrEnum):
    """How a research source is polled."""

    RSS = "RSS"
    GITHUB_RELEASES = "GITHUB_RELEASES"
    OFFICIAL_BLOG = "OFFICIAL_BLOG"
    LLM_SEARCH = "LLM_SEARCH"
    AGGREGATOR = "AGGREGATOR"


class TrustLevel(StrEnum):
    """How much weight a source's claim carries.

    Ordered by authority; :func:`trust_authority_score` maps them onto 0-100.
    """

    OFFICIAL = "OFFICIAL"
    PRIMARY = "PRIMARY"
    REPUTABLE_SECONDARY = "REPUTABLE_SECONDARY"
    AGGREGATOR = "AGGREGATOR"
    UNKNOWN = "UNKNOWN"


TRUST_AUTHORITY: dict[TrustLevel, int] = {
    TrustLevel.OFFICIAL: 100,
    TrustLevel.PRIMARY: 85,
    TrustLevel.REPUTABLE_SECONDARY: 60,
    TrustLevel.AGGREGATOR: 35,
    TrustLevel.UNKNOWN: 15,
}

# Trust levels that can, on their own, establish a fact (used from PHASE 5).
AUTHORITATIVE_TRUST = frozenset({TrustLevel.OFFICIAL, TrustLevel.PRIMARY})


def trust_authority_score(trust: TrustLevel) -> int:
    """Authority component value for a trust level."""
    return TRUST_AUTHORITY[trust]


class ResearchItemStatus(StrEnum):
    """Lifecycle of a single research item.

    REJECTED is reserved for irreversible outcomes (irrelevant, spam, unsafe,
    manually excluded). A low score never lands here — it becomes FILTERED,
    which stays eligible for re-scoring when new evidence arrives.
    """

    NEW = "NEW"
    CLUSTERED = "CLUSTERED"
    FILTERED = "FILTERED"
    SELECTED = "SELECTED"
    BACKLOG = "BACKLOG"
    VERIFIED = "VERIFIED"
    CONVERTED_TO_CONTENT = "CONVERTED_TO_CONTENT"
    REJECTED = "REJECTED"


class ClusterStatus(StrEnum):
    """Lifecycle of one information event.

    A cluster is never closed by a weak score alone: it can gain supporting
    items later and be re-scored, so FILTERED and BACKLOG are both revisitable.
    """

    OPEN = "OPEN"
    SCORED = "SCORED"
    SELECTED = "SELECTED"
    BACKLOG = "BACKLOG"
    FILTERED = "FILTERED"
    VERIFIED = "VERIFIED"
    CONTENT_CREATED = "CONTENT_CREATED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


# Statuses a re-score is allowed to move a cluster out of.
RESCORABLE_CLUSTER_STATUSES = frozenset(
    {ClusterStatus.OPEN, ClusterStatus.SCORED, ClusterStatus.BACKLOG, ClusterStatus.FILTERED}
)


class RejectionReason(StrEnum):
    """Why a cluster or item was rejected for good."""

    IRRELEVANT = "IRRELEVANT"
    SPAM = "SPAM"
    UNSAFE_TOPIC = "UNSAFE_TOPIC"
    CONFIRMED_JUNK = "CONFIRMED_JUNK"
    MANUAL_EXCLUSION = "MANUAL_EXCLUSION"


class EventType(StrEnum):
    """What kind of information event a cluster represents."""

    RELEASE = "RELEASE"
    UPDATE = "UPDATE"
    NEW_TOOL = "NEW_TOOL"
    RESEARCH = "RESEARCH"
    GUIDE = "GUIDE"
    INCIDENT = "INCIDENT"
    OTHER = "OTHER"


class ClusterItemRole(StrEnum):
    """Role of an item inside its cluster."""

    PRIMARY = "PRIMARY"
    SUPPORTING = "SUPPORTING"
    DUPLICATE = "DUPLICATE"


class MatchMethod(StrEnum):
    """Which deduplication layer attached an item to its cluster."""

    URL = "URL"
    FINGERPRINT = "FINGERPRINT"
    SIMHASH = "SIMHASH"
    ENTITY = "ENTITY"
    LLM = "LLM"
    SEED = "SEED"


class VerificationStatus(StrEnum):
    """Fact-check outcome (populated from PHASE 5)."""

    UNVERIFIED = "UNVERIFIED"
    SUPPORTED = "SUPPORTED"
    CONFLICTING = "CONFLICTING"
    INSUFFICIENT = "INSUFFICIENT"


class PipelineJob(StrEnum):
    """Named unit of scheduled work."""

    RESEARCH_POLL = "RESEARCH_POLL"
    RESEARCH_CLUSTER = "RESEARCH_CLUSTER"
    TOPIC_SCORE = "TOPIC_SCORE"


class RunStatus(StrEnum):
    """Outcome of one pipeline run."""

    RUNNING = "RUNNING"
    SUCCESS = "SUCCESS"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    COST_LIMIT_REACHED = "COST_LIMIT_REACHED"


class AiTaskType(StrEnum):
    """What an LLM call was for — drives model routing and cost reporting."""

    EXTRACTION = "EXTRACTION"
    CLASSIFICATION = "CLASSIFICATION"
    SCORING = "SCORING"
    DEDUP_ADJUDICATION = "DEDUP_ADJUDICATION"
    SEARCH = "SEARCH"
    SYNTHESIS = "SYNTHESIS"
    # Writing the analysis paragraph of a post (PHASE 5). Separate from
    # SYNTHESIS so content generation can be costed and audited on its own.
    CONTENT_DRAFT = "CONTENT_DRAFT"


class PublishMode(StrEnum):
    """Whether publishing actually reaches the platform.

    DRY_RUN is the default everywhere: an autonomous publisher that cannot be
    run harmlessly is one nobody dares to run at all.
    """

    DRY_RUN = "DRY_RUN"
    LIVE = "LIVE"


class PublicationStatus(StrEnum):
    """Outcome of one publication attempt."""

    SIMULATED = "SIMULATED"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"


class AiCallStatus(StrEnum):
    """Outcome of one LLM call."""

    SUCCESS = "SUCCESS"
    ERROR = "ERROR"
    BLOCKED_BY_COST_LIMIT = "BLOCKED_BY_COST_LIMIT"
