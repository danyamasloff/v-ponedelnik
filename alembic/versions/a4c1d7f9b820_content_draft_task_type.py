"""content draft task type

Revision ID: a4c1d7f9b820
Revises: c39ac4cbe263
Create Date: 2026-09-05 20:05:00.000000

Adds the CONTENT_DRAFT value to the ai_task_type enum so PHASE 5 generation is
costed and audited separately from research synthesis.
"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a4c1d7f9b820"
down_revision: str | Sequence[str] | None = "c39ac4cbe263"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the new enum value."""
    # PostgreSQL 12+ allows ADD VALUE inside a transaction as long as the value
    # is not used in that same transaction — which it is not here.
    op.execute("ALTER TYPE ai_task_type ADD VALUE IF NOT EXISTS 'CONTENT_DRAFT'")


def downgrade() -> None:
    """Remove the value by rebuilding the type.

    PostgreSQL cannot drop a single enum value, so the type is recreated
    without it. Rows already using CONTENT_DRAFT would block this, and that is
    the correct outcome: dropping them silently would erase spend history.
    """
    op.execute("ALTER TYPE ai_task_type RENAME TO ai_task_type_old")
    op.execute(
        "CREATE TYPE ai_task_type AS ENUM "
        "('EXTRACTION', 'CLASSIFICATION', 'SCORING', 'DEDUP_ADJUDICATION', "
        "'SEARCH', 'SYNTHESIS')"
    )
    op.execute(
        "ALTER TABLE ai_generations ALTER COLUMN task TYPE ai_task_type "
        "USING task::text::ai_task_type"
    )
    op.execute("DROP TYPE ai_task_type_old")
