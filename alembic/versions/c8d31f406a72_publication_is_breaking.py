"""publication is_breaking

Revision ID: c8d31f406a72
Revises: b7e2c05af913
Create Date: 2026-09-06 19:40:00.000000

Breaking posts jump the schedule, so they need their own daily ceiling — which
means the publication log has to remember which posts were breaking.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c8d31f406a72"
down_revision: str | Sequence[str] | None = "b7e2c05af913"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the flag, defaulting existing rows to "not breaking"."""
    op.add_column(
        "publications",
        sa.Column("is_breaking", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    """Drop it. Nothing else references the column."""
    op.drop_column("publications", "is_breaking")
