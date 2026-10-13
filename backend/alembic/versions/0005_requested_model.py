"""Record the provider and model each answer was requested with.

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-13

Phase 4.1 lets a question choose among several providers and models. ``provider`` and
``model`` keep meaning "what was called" (unset when no model was called, for example when
no passage matched); the new ``requested_provider`` and ``requested_model`` record the
choice itself. Existing answers are backfilled from ``provider`` and ``model``; answers
stored without a model call keep NULL, because Phase 4 did not record a choice for them.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("messages", sa.Column("requested_provider", sa.String(32)))
    op.add_column("messages", sa.Column("requested_model", sa.String(128)))
    op.execute(
        "UPDATE messages SET requested_provider = provider, requested_model = model "
        "WHERE role = 'assistant'"
    )


def downgrade() -> None:
    op.drop_column("messages", "requested_model")
    op.drop_column("messages", "requested_provider")
