"""Add S3 connectors, their sync runs and the objects they imported.

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-17

Phase 5 imports documents from Amazon S3 into the existing ingestion pipeline:

* ``connectors``: one bucket and prefix each. No credentials are stored; the worker uses
  the standard AWS credential chain of its environment.
* ``connector_sync_runs``: the sync queue and its history. A run is claimed by one worker
  under a lease, exactly like an ingestion job, and a partial unique index allows at most
  one queued or running sync per connector.
* ``connector_objects``: one row per S3 object, mapping it to its document and recording
  the version (ETag, size, last-modified time, version ID) that was last imported.

``documents.source_type`` marks imported documents. Content de-duplication now applies to
manual uploads only, because two S3 objects with the same bytes are two documents. Existing
rows are all uploads, so the old unique constraint and the new partial index accept exactly
the same existing data.

The downgrade refuses to run while imported documents exist (they could violate the old
unique constraint); delete the connectors first.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TIMESTAMP = sa.DateTime(timezone=True)
_UUID = postgresql.UUID(as_uuid=True)
_RUN_STATUSES = "'pending', 'running', 'succeeded', 'partial', 'failed', 'cancelled'"


def upgrade() -> None:
    op.add_column(
        "documents",
        sa.Column("source_type", sa.String(16), nullable=False, server_default="upload"),
    )
    op.create_check_constraint(
        "ck_documents_source_type", "documents", "source_type IN ('upload', 's3')"
    )
    op.create_index(
        "uq_documents_upload_sha256",
        "documents",
        ["sha256"],
        unique=True,
        postgresql_where=sa.text("source_type = 'upload'"),
    )
    op.drop_constraint("uq_documents_sha256", "documents", type_="unique")

    op.create_table(
        "connectors",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("provider", sa.String(16), nullable=False, server_default="s3"),
        sa.Column("bucket", sa.String(63), nullable=False),
        sa.Column("prefix", sa.String(512), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("sync_interval_minutes", sa.Integer(), nullable=False),
        sa.Column("next_sync_at", _TIMESTAMP),
        sa.Column("last_sync_started_at", _TIMESTAMP),
        sa.Column("last_sync_finished_at", _TIMESTAMP),
        sa.Column("last_success_at", _TIMESTAMP),
        sa.Column("last_sync_status", sa.String(16)),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("last_error_message", sa.Text()),
        sa.Column("created_at", _TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", _TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("provider IN ('s3')", name="ck_connectors_provider"),
        sa.CheckConstraint("length(btrim(name)) > 0", name="ck_connectors_name_not_empty"),
        sa.CheckConstraint(
            "sync_interval_minutes BETWEEN 5 AND 10080", name="ck_connectors_sync_interval"
        ),
        sa.CheckConstraint(
            f"last_sync_status IS NULL OR last_sync_status IN ({_RUN_STATUSES})",
            name="ck_connectors_last_sync_status",
        ),
        sa.CheckConstraint(
            "enabled OR next_sync_at IS NULL", name="ck_connectors_disabled_unscheduled"
        ),
        sa.UniqueConstraint("provider", "bucket", "prefix", name="uq_connectors_source"),
    )
    op.create_index("uq_connectors_name", "connectors", [sa.text("lower(name)")], unique=True)
    op.create_index(
        "ix_connectors_due",
        "connectors",
        ["next_sync_at"],
        postgresql_where=sa.text("next_sync_at IS NOT NULL"),
    )

    op.create_table(
        "connector_sync_runs",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "connector_id",
            _UUID,
            sa.ForeignKey("connectors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("trigger", sa.String(16), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("run_after", _TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.Column(
            "cancel_requested", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("locked_by", sa.String(128)),
        sa.Column("lease_token", _UUID),
        sa.Column("lease_expires_at", _TIMESTAMP),
        sa.Column("heartbeat_at", _TIMESTAMP),
        sa.Column("objects_scanned", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("documents_imported", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("documents_updated", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "documents_unchanged", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("documents_removed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("objects_skipped", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("objects_failed", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column(
            "failures",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_message", sa.Text()),
        sa.Column("request_id", sa.String(64)),
        sa.Column("created_at", _TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", _TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", _TIMESTAMP),
        sa.Column("finished_at", _TIMESTAMP),
        sa.CheckConstraint(f"status IN ({_RUN_STATUSES})", name="ck_connector_sync_runs_status"),
        sa.CheckConstraint(
            "trigger IN ('manual', 'scheduled')", name="ck_connector_sync_runs_trigger"
        ),
        sa.CheckConstraint(
            "attempts >= 0 AND attempts <= max_attempts", name="ck_connector_sync_runs_attempts"
        ),
        sa.CheckConstraint("max_attempts >= 1", name="ck_connector_sync_runs_max_attempts"),
        sa.CheckConstraint(
            "status <> 'running' OR (lease_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND locked_by IS NOT NULL)",
            name="ck_connector_sync_runs_running_has_lease",
        ),
        sa.CheckConstraint(
            "objects_scanned >= 0 AND documents_imported >= 0 AND documents_updated >= 0 "
            "AND documents_unchanged >= 0 AND documents_removed >= 0 AND objects_skipped >= 0 "
            "AND objects_failed >= 0",
            name="ck_connector_sync_runs_counts",
        ),
    )
    op.create_index(
        "uq_connector_sync_runs_one_active",
        "connector_sync_runs",
        ["connector_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )
    op.create_index(
        "ix_connector_sync_runs_claimable",
        "connector_sync_runs",
        ["run_after", "created_at"],
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_connector_sync_runs_expired_leases",
        "connector_sync_runs",
        ["lease_expires_at"],
        postgresql_where=sa.text("status = 'running'"),
    )
    op.create_index(
        "ix_connector_sync_runs_connector_created",
        "connector_sync_runs",
        ["connector_id", "created_at"],
    )

    op.create_table(
        "connector_objects",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column(
            "connector_id",
            _UUID,
            sa.ForeignKey("connectors.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("object_key", sa.Text(), nullable=False),
        sa.Column("document_id", _UUID, sa.ForeignKey("documents.id", ondelete="SET NULL")),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("etag", sa.String(128)),
        sa.Column("version_id", sa.String(1024)),
        sa.Column("size_bytes", sa.BigInteger()),
        sa.Column("last_modified", _TIMESTAMP),
        sa.Column("last_error_code", sa.String(64)),
        sa.Column("last_error_message", sa.Text()),
        sa.Column("first_seen_at", _TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.Column("last_seen_at", _TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.Column("last_synced_at", _TIMESTAMP),
        sa.Column("deleted_at", _TIMESTAMP),
        sa.Column("updated_at", _TIMESTAMP, nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("connector_id", "object_key", name="uq_connector_objects_key"),
        sa.CheckConstraint(
            "status IN ('active', 'failed', 'deleted')", name="ck_connector_objects_status"
        ),
        sa.CheckConstraint("length(object_key) > 0", name="ck_connector_objects_key_not_empty"),
        sa.CheckConstraint(
            "status <> 'deleted' OR (document_id IS NULL AND deleted_at IS NOT NULL)",
            name="ck_connector_objects_deleted_shape",
        ),
    )
    op.create_index(
        "uq_connector_objects_document",
        "connector_objects",
        ["document_id"],
        unique=True,
        postgresql_where=sa.text("document_id IS NOT NULL"),
    )


def downgrade() -> None:
    imported = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM documents WHERE source_type <> 'upload'")
    )
    if imported:
        raise RuntimeError(
            f"{imported} imported documents exist; delete the connectors (which removes their "
            "documents) before downgrading below 0006."
        )
    op.drop_table("connector_objects")
    op.drop_table("connector_sync_runs")
    op.drop_table("connectors")
    op.create_unique_constraint("uq_documents_sha256", "documents", ["sha256"])
    op.drop_index("uq_documents_upload_sha256", table_name="documents")
    op.drop_constraint("ck_documents_source_type", "documents", type_="check")
    op.drop_column("documents", "source_type")
