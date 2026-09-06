"""niche score run platform

Revision ID: 3153839f70f1
Revises: 6f3f7edcb738
Create Date: 2026-09-02 14:36:21.331640

"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = '3153839f70f1'
down_revision: str | Sequence[str] | None = '6f3f7edcb738'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# The 'platform' ENUM type already exists (created with market_channels).
# Autogenerate emits a plain sa.Enum, which would try to CREATE TYPE again and
# fail, so the existing type is referenced explicitly with create_type=False.
platform_enum = postgresql.ENUM(
    'TELEGRAM', 'MAX', 'UNKNOWN', name='platform', create_type=False
)


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('niche_score_runs', sa.Column('platform', platform_enum, nullable=True))
    op.create_index(
        op.f('ix_niche_score_runs_platform'), 'niche_score_runs', ['platform'], unique=False
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f('ix_niche_score_runs_platform'), table_name='niche_score_runs')
    op.drop_column('niche_score_runs', 'platform')
    # The ENUM type itself is NOT dropped here: market_channels still uses it.
