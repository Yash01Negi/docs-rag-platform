from collections.abc import Iterator

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect, text

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
