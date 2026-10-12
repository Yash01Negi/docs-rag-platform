from collections.abc import Iterator

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, text

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
