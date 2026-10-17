from collections.abc import Iterator

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.db.models import Base

pytestmark = pytest.mark.integration


@pytest.fixture
def alembic_config(empty_database_url: str, monkeypatch: pytest.MonkeyPatch) -> Iterator[Config]:
    monkeypatch.setenv("DATABASE_URL", empty_database_url)
    yield Config("alembic.ini")


def _vector_installed(url: str) -> bool:
    engine = create_engine(url)
    try:
        with engine.connect() as connection:
            return bool(
                connection.scalar(text("SELECT 1 FROM pg_extension WHERE extname = 'vector'"))
            )
    finally:
        engine.dispose()


def test_upgrade_enables_and_downgrade_removes_pgvector(
    alembic_config: Config,
    empty_database_url: str,
) -> None:
    assert not _vector_installed(empty_database_url)

    command.upgrade(alembic_config, "head")
    assert _vector_installed(empty_database_url)

    command.downgrade(alembic_config, "base")
    assert not _vector_installed(empty_database_url)


def _tables(url: str) -> set[str]:
    engine = create_engine(url)
    try:
        return set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_document_tables_upgrade_and_downgrade(
    alembic_config: Config, empty_database_url: str
) -> None:
    command.upgrade(alembic_config, "head")
    assert {"documents", "chunks", "ingestion_jobs"} <= _tables(empty_database_url)

    engine = create_engine(empty_database_url)
    try:
        with engine.connect() as connection:
            embedding_type = connection.scalar(
                text(
                    "SELECT format_type(atttypid, atttypmod) FROM pg_attribute "
                    "WHERE attrelid = 'chunks'::regclass AND attname = 'embedding'"
                )
            )
    finally:
        engine.dispose()
    assert embedding_type == "vector(1024)"

    command.downgrade(alembic_config, "0001")
    assert not {"documents", "chunks", "ingestion_jobs"} & _tables(empty_database_url)
    assert _vector_installed(empty_database_url)


def test_orm_models_match_the_migrated_schema(
    alembic_config: Config, empty_database_url: str
) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(empty_database_url)
    try:
        with engine.connect() as connection:
            context = MigrationContext.configure(connection)
            differences = compare_metadata(context, Base.metadata)
    finally:
        engine.dispose()
    assert differences == []


def test_keyword_search_migration_backfills_existing_chunks(
    alembic_config: Config, empty_database_url: str
) -> None:
    command.upgrade(alembic_config, "0002")
    engine = create_engine(empty_database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO documents (id, filename, file_type, mime_type, size_bytes, "
                    "sha256, storage_key, status, chunk_count, embedding_model, "
                    "embedding_model_revision, embedding_dimension, processed_at) VALUES "
                    "(gen_random_uuid(), 'a.txt', 'text', 'text/plain', 10, repeat('a', 64), "
                    "'key', 'completed', 1, 'm', 'r', 1024, now())"
                )
            )
            connection.execute(
                text(
                    "INSERT INTO chunks (id, document_id, ordinal, text, token_count, language, "
                    "embedding) SELECT gen_random_uuid(), id, 0, '文書を検索します。Searching "
                    "documents', 5, 'mixed', array_fill(0.03125, ARRAY[1024])::vector "
                    "FROM documents"
                )
            )
        command.upgrade(alembic_config, "head")
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT search_vector::text AS v, search_length FROM chunks")
            ).one()
    finally:
        engine.dispose()
    assert row.v == "'document':8 'search':7 '文書':1 '検索':3"
    assert row.search_length == 4

    command.downgrade(alembic_config, "0002")


def test_conversation_tables_upgrade_and_downgrade_keep_documents(
    alembic_config: Config, empty_database_url: str
) -> None:
    command.upgrade(alembic_config, "0003")
    engine = create_engine(empty_database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO documents (id, filename, file_type, mime_type, size_bytes, "
                    "sha256, storage_key, status) VALUES (gen_random_uuid(), 'a.txt', 'text', "
                    "'text/plain', 10, repeat('b', 64), 'key2', 'pending')"
                )
            )
        command.upgrade(alembic_config, "head")
        assert {"conversations", "messages", "message_citations"} <= _tables(empty_database_url)
        with engine.begin() as connection:
            conversation = connection.scalar(
                text(
                    "INSERT INTO conversations (id, title) "
                    "VALUES (gen_random_uuid(), 't') RETURNING id"
                )
            )
            question = connection.scalar(
                text(
                    "INSERT INTO messages (id, conversation_id, sequence, role, content, status) "
                    "VALUES (gen_random_uuid(), :c, 1, 'user', 'q', 'complete') RETURNING id"
                ),
                {"c": conversation},
            )
            # A second streaming answer in the same conversation violates the partial index.
            connection.execute(
                text(
                    "INSERT INTO messages (id, conversation_id, sequence, role, status, "
                    "reply_to_id) VALUES (gen_random_uuid(), :c, 2, 'assistant', 'streaming', :q)"
                ),
                {"c": conversation, "q": question},
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO messages (id, conversation_id, sequence, role, content, status) "
                    "VALUES (gen_random_uuid(), :c, 3, 'user', 'q2', 'complete')"
                ),
                {"c": conversation},
            )
            connection.execute(
                text(
                    "INSERT INTO messages (id, conversation_id, sequence, role, status, "
                    "reply_to_id) SELECT gen_random_uuid(), :c, 4, 'assistant', 'streaming', id "
                    "FROM messages WHERE sequence = 3"
                ),
                {"c": conversation},
            )
        command.downgrade(alembic_config, "0003")
        assert not {"conversations", "messages", "message_citations"} & _tables(empty_database_url)
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM documents")) == 1
    finally:
        engine.dispose()


_INSERT_DOCUMENT = (
    "INSERT INTO documents (id, filename, file_type, mime_type, size_bytes, sha256, "
    "storage_key, status{extra_columns}) VALUES (gen_random_uuid(), 'a.txt', 'text', "
    "'text/plain', 10, repeat('c', 64), :key, 'pending'{extra_values})"
)


def _insert_document(connection, key: str, source_type: str | None = None) -> None:  # type: ignore[no-untyped-def]
    sql = _INSERT_DOCUMENT.format(
        extra_columns=", source_type" if source_type else "",
        extra_values=", :source" if source_type else "",
    )
    connection.execute(text(sql), {"key": key, "source": source_type})


def test_connector_migration_keeps_documents_and_upload_deduplication(
    alembic_config: Config, empty_database_url: str
) -> None:
    command.upgrade(alembic_config, "0005")
    engine = create_engine(empty_database_url)
    try:
        with engine.begin() as connection:
            _insert_document(connection, "key-before")
        command.upgrade(alembic_config, "0006")
        assert {"connectors", "connector_sync_runs", "connector_objects"} <= _tables(
            empty_database_url
        )
        with engine.connect() as connection:
            # Existing documents become manual uploads.
            assert connection.scalar(text("SELECT source_type FROM documents")) == "upload"
        # Identical uploads are still rejected ...
        with pytest.raises(IntegrityError), engine.begin() as connection:
            _insert_document(connection, "key-dup")
        # ... but an imported document may share content with an upload or another import.
        with engine.begin() as connection:
            _insert_document(connection, "key-s3-1", "s3")
            _insert_document(connection, "key-s3-2", "s3")
        with pytest.raises(IntegrityError), engine.begin() as connection:
            _insert_document(connection, "key-bad", "dropbox")

        # The downgrade refuses to run while imported documents exist ...
        with pytest.raises(RuntimeError, match="imported documents"):
            command.downgrade(alembic_config, "0005")
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM documents WHERE source_type = 's3'"))
        # ... and otherwise restores the previous schema without touching uploads.
        command.downgrade(alembic_config, "0005")
        assert not {"connectors", "connector_sync_runs", "connector_objects"} & _tables(
            empty_database_url
        )
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM documents")) == 1
        command.upgrade(alembic_config, "head")
    finally:
        engine.dispose()


def test_connector_constraints(alembic_config: Config, empty_database_url: str) -> None:
    command.upgrade(alembic_config, "head")
    engine = create_engine(empty_database_url)
    insert_connector = text(
        "INSERT INTO connectors (id, name, bucket, prefix, sync_interval_minutes) "
        "VALUES (gen_random_uuid(), :name, :bucket, :prefix, :interval) RETURNING id"
    )
    insert_run = text(
        "INSERT INTO connector_sync_runs (id, connector_id, trigger, status, max_attempts) "
        "VALUES (gen_random_uuid(), :c, 'manual', :status, 3)"
    )
    try:
        with engine.begin() as connection:
            connector = connection.scalar(
                insert_connector, {"name": "Docs", "bucket": "docs", "prefix": "", "interval": 60}
            )
            connection.execute(insert_run, {"c": connector, "status": "pending"})
            connection.execute(insert_run, {"c": connector, "status": "succeeded"})
        bad = [
            (insert_connector, {"name": "docs", "bucket": "other", "prefix": "", "interval": 60}),
            (insert_connector, {"name": "Two", "bucket": "docs", "prefix": "", "interval": 60}),
            (insert_connector, {"name": "Fast", "bucket": "b2", "prefix": "", "interval": 1}),
            (insert_connector, {"name": " ", "bucket": "b3", "prefix": "", "interval": 60}),
            # A second active run for the same connector.
            (insert_run, {"c": connector, "status": "running"}),
            (insert_run, {"c": connector, "status": "pending"}),
        ]
        for statement, params in bad:
            with pytest.raises(IntegrityError), engine.begin() as connection:
                connection.execute(statement, params)
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO connector_objects (id, connector_id, object_key, status) "
                    "VALUES (gen_random_uuid(), :c, 'a.txt', 'active')"
                ),
                {"c": connector},
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO connector_objects (id, connector_id, object_key, status) "
                    "VALUES (gen_random_uuid(), :c, 'a.txt', 'active')"
                ),
                {"c": connector},
            )
        with pytest.raises(IntegrityError), engine.begin() as connection:
            # A deleted object must record when, and no longer point at a document.
            connection.execute(
                text(
                    "INSERT INTO connector_objects (id, connector_id, object_key, status) "
                    "VALUES (gen_random_uuid(), :c, 'b.txt', 'deleted')"
                ),
                {"c": connector},
            )
        with engine.begin() as connection:
            connection.execute(text("DELETE FROM connectors"))
            assert connection.scalar(text("SELECT count(*) FROM connector_sync_runs")) == 0
            assert connection.scalar(text("SELECT count(*) FROM connector_objects")) == 0
    finally:
        engine.dispose()
