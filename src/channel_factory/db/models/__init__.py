"""ORM models.

Every model must be imported here: Alembic autogenerate only sees tables that
are registered on ``Base.metadata`` at import time.
"""

from channel_factory.db.base import Base
from channel_factory.db.models.competitor import CompetitorWatch
from channel_factory.db.models.direct_import import DirectImport, DirectImportRow
from channel_factory.db.models.market import MarketChannel, MarketSnapshot
from channel_factory.db.models.niche import Niche
from channel_factory.db.models.niche_score import NicheScore, NicheScoreRun
from channel_factory.db.models.pipeline import AiGeneration, PipelineRun
from channel_factory.db.models.publication import Publication
from channel_factory.db.models.research import (
    ResearchCluster,
    ResearchClusterItem,
    ResearchItem,
    ResearchSource,
    TopicScore,
)
from channel_factory.db.models.source import Source

__all__ = [
    "AiGeneration",
    "Base",
    "CompetitorWatch",
    "DirectImport",
    "DirectImportRow",
    "MarketChannel",
    "MarketSnapshot",
    "Niche",
    "NicheScore",
    "NicheScoreRun",
    "PipelineRun",
    "Publication",
    "ResearchCluster",
    "ResearchClusterItem",
    "ResearchItem",
    "ResearchSource",
    "Source",
    "TopicScore",
]
