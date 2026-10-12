"""Add conversations, messages and message_citations.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-12

Chat history (Phase 4). The constraints encode:

* message order: ``sequence`` is unique within a conversation;
* one answer per question (``reply_to_id`` is unique) and at most one answer being
  generated per conversation (a partial unique index on ``status = 'streaming'``);
* idempotent questions: ``client_message_id`` is unique within a conversation, so a client
  retry never stores a question twice.

Citations keep the chunk and document IDs without foreign keys, plus a snapshot of the
passage's location and a hash of its text, so they stay readable after the document is
reindexed (new chunk IDs) or deleted. API keys, provider reasoning and raw provider errors
are never stored.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TIMESTAMP = sa.DateTime(timezone=True)
_UUID = postgresql.UUID(as_uuid=True)


def upgrade() -> None:
    op.create_table(
        "conversations",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("title_is_default", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", _TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", _TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("length(btrim(title)) > 0", name="ck_conversations_title_not_empty"),
    )
    op.create_index("ix_conversations_updated_at_id", "conversations", ["updated_at", "id"])

    op.create_table(
        "messages",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "conversation_id",
            _UUID,
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("client_message_id", _UUID, nullable=True),
        sa.Column(
            "reply_to_id", _UUID, sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column("grounding", sa.String(32), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("provider", sa.String(32), nullable=True),
        sa.Column("model", sa.String(128), nullable=True),
        sa.Column("served_model", sa.String(128), nullable=True),
        sa.Column("stop_reason", sa.String(32), nullable=True),
        sa.Column("provider_request_id", sa.String(128), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=True),
        sa.Column("output_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_read_input_tokens", sa.Integer(), nullable=True),
        sa.Column("cache_creation_input_tokens", sa.Integer(), nullable=True),
        sa.Column("retrieval_mode", sa.String(16), nullable=True),
        sa.Column("retrieval_ms", sa.Float(), nullable=True),
        sa.Column("first_token_ms", sa.Float(), nullable=True),
        sa.Column("total_ms", sa.Float(), nullable=True),
        sa.Column("request_id", sa.String(64), nullable=True),
        sa.Column("created_at", _TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", _TIMESTAMP, nullable=True),
        sa.UniqueConstraint(
            "conversation_id", "sequence", name="uq_messages_conversation_sequence"
        ),
        sa.CheckConstraint("sequence >= 1", name="ck_messages_sequence"),
        sa.CheckConstraint("role IN ('user', 'assistant')", name="ck_messages_role"),
        sa.CheckConstraint(
            "status IN ('complete', 'streaming', 'failed', 'cancelled')",
            name="ck_messages_status",
        ),
        sa.CheckConstraint(
            "grounding IS NULL OR grounding IN "
            "('grounded', 'insufficient_evidence', 'no_sources', 'uncited')",
            name="ck_messages_grounding",
        ),
        sa.CheckConstraint(
            "role <> 'user' OR (status = 'complete' AND reply_to_id IS NULL "
            "AND length(content) > 0)",
            name="ck_messages_user_shape",
        ),
        sa.CheckConstraint(
            "role <> 'assistant' OR (reply_to_id IS NOT NULL AND client_message_id IS NULL)",
            name="ck_messages_assistant_shape",
        ),
        sa.CheckConstraint(
            "(input_tokens IS NULL OR input_tokens >= 0) AND "
            "(output_tokens IS NULL OR output_tokens >= 0)",
            name="ck_messages_tokens",
        ),
    )
    op.create_index(
        "uq_messages_client_message_id",
        "messages",
        ["conversation_id", "client_message_id"],
        unique=True,
        postgresql_where=sa.text("client_message_id IS NOT NULL"),
    )
    op.create_index(
        "uq_messages_reply_to",
        "messages",
        ["reply_to_id"],
        unique=True,
        postgresql_where=sa.text("reply_to_id IS NOT NULL"),
    )
    op.create_index(
        "uq_messages_one_streaming_per_conversation",
        "messages",
        ["conversation_id"],
        unique=True,
        postgresql_where=sa.text("status = 'streaming'"),
    )

    op.create_table(
        "message_citations",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "message_id", _UUID, sa.ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("source_index", sa.Integer(), nullable=False),
        sa.Column("supporting", sa.Boolean(), nullable=False),
        sa.Column("chunk_id", _UUID, nullable=False),
        sa.Column("document_id", _UUID, nullable=False),
        sa.Column("filename", sa.String(255), nullable=False),
        sa.Column("chunk_ordinal", sa.Integer(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("section", sa.Text(), nullable=True),
        sa.Column("language", sa.String(16), nullable=False),
        sa.Column("retrieval_rank", sa.Integer(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("score_type", sa.String(32), nullable=False),
        sa.Column("chunk_text_sha256", sa.String(64), nullable=False),
        sa.Column("excerpt", sa.Text(), nullable=True),
        sa.Column("created_at", _TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("message_id", "source_index", name="uq_message_citations_source"),
        sa.CheckConstraint("source_index >= 1", name="ck_message_citations_source_index"),
        sa.CheckConstraint("chunk_ordinal >= 0", name="ck_message_citations_ordinal"),
        sa.CheckConstraint(
            "chunk_text_sha256 ~ '^[0-9a-f]{64}$'", name="ck_message_citations_sha256_hex"
        ),
    )
    op.create_index("ix_message_citations_chunk_id", "message_citations", ["chunk_id"])
    op.create_index("ix_message_citations_document_id", "message_citations", ["document_id"])


def downgrade() -> None:
    op.drop_table("message_citations")
    op.drop_table("messages")
    op.drop_table("conversations")
