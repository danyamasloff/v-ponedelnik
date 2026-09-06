"""publication topic key

Revision ID: b7e2c05af913
Revises: a4c1d7f9b820
Create Date: 2026-09-06 15:10:00.000000

Own posts (the evergreen track) have no research cluster behind them, so
``research_cluster_id`` cannot answer "have we published this already". They
are identified by their topic key from config/evergreen_topics.yaml instead.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e2c05af913"
down_revision: str | Sequence[str] | None = "a4c1d7f9b820"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the topic key used to deduplicate own posts."""
    op.add_column("publications", sa.Column("topic_key", sa.String(length=160), nullable=True))
    op.create_index(op.f("ix_publications_topic_key"), "publications", ["topic_key"])


def downgrade() -> None:
    """Drop it. Nothing else references the column."""
    op.drop_index(op.f("ix_publications_topic_key"), table_name="publications")
    op.drop_column("publications", "topic_key")
