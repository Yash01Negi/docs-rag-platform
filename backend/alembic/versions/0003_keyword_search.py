"""Add keyword-search vectors to chunks.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-12

``chunks.search_vector`` holds the terms produced by the bilingual analyzer
(``app/search/analyzer.py``: SudachiPy for Japanese, Snowball for English) as a ``tsvector``
with term positions, and ``chunks.search_length`` the number of terms, used by BM25 length
normalisation. A GIN index serves term and phrase lookups. Existing chunks are backfilled
with the same analyzer the worker uses for new ones.

No approximate-nearest-neighbour index is created on ``chunks.embedding``: exact search was
benchmarked against HNSW and is kept at the current scale (ADR 0006).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

from app.search.lexical_index import rebuild_lexical_index

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("chunks", sa.Column("search_vector", postgresql.TSVECTOR(), nullable=True))
    op.add_column("chunks", sa.Column("search_length", sa.Integer(), nullable=True))
    rebuild_lexical_index(op.get_bind())
    op.alter_column("chunks", "search_vector", nullable=False)
    op.alter_column("chunks", "search_length", nullable=False)
    op.create_check_constraint("ck_chunks_search_length", "chunks", "search_length >= 0")
    op.create_index(
        "ix_chunks_search_vector", "chunks", ["search_vector"], postgresql_using="gin"
    )


def downgrade() -> None:
    op.drop_index("ix_chunks_search_vector", table_name="chunks")
    op.drop_constraint("ck_chunks_search_length", "chunks", type_="check")
    op.drop_column("chunks", "search_length")
    op.drop_column("chunks", "search_vector")
