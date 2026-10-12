from collections.abc import Callable

import pytest
from pydantic import ValidationError

from app.core.config import Environment, Settings


def test_settings_are_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@db:5432/yashrag")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("LOG_LEVEL", "warning")
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://localhost:5173, https://app.example.com/")

    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.environment is Environment.PRODUCTION
    assert settings.log_level == "WARNING"
    assert settings.cors_allowed_origins == ["http://localhost:5173", "https://app.example.com"]
    assert settings.database_url.get_secret_value().endswith("@db:5432/yashrag")


def test_database_url_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)

    with pytest.raises(ValidationError, match="database_url"):
        Settings(_env_file=None)  # type: ignore[call-arg]


def test_database_url_is_not_exposed_in_repr(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings(database_url="postgresql+psycopg://u:s3cret@db/yashrag")

    assert "s3cret" not in repr(settings)


def test_database_url_must_use_psycopg_driver(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(ValidationError, match="postgresql\\+psycopg"):
        make_settings(database_url="postgresql://u:p@db/yashrag")


def test_cors_defaults_to_no_origins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CORS_ALLOWED_ORIGINS", raising=False)

    settings = Settings(_env_file=None, database_url="postgresql+psycopg://u:p@db/x")  # type: ignore[arg-type]

    assert settings.cors_allowed_origins == []


@pytest.mark.parametrize("origins", ["*", "http://localhost:5173,*", "localhost:5173"])
def test_invalid_cors_origins_are_rejected(
    make_settings: Callable[..., Settings], origins: str
) -> None:
    with pytest.raises(ValidationError, match="cors_allowed_origins"):
        make_settings(cors_allowed_origins=origins)


def test_invalid_log_level_is_rejected(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(ValidationError, match="log_level"):
        make_settings(log_level="verbose")


def test_invalid_environment_is_rejected(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(ValidationError, match="environment"):
        make_settings(environment="staging")


def test_phase2_defaults(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings(embedding_provider="bge-m3")

    assert settings.max_upload_bytes == 20 * 1024 * 1024
    assert settings.embedding_model_name == "BAAI/bge-m3"
    assert settings.embedding_dimension == 1024
    assert (settings.chunk_target_tokens, settings.chunk_overlap_tokens) == (400, 60)
    assert settings.chunk_max_tokens == 440


def test_fake_embeddings_require_test_environment(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(ValidationError, match="only allowed when ENVIRONMENT=test"):
        make_settings(environment="development", embedding_provider="fake")


def test_embedding_dimension_must_match_schema(make_settings: Callable[..., Settings]) -> None:
    with pytest.raises(ValidationError, match="1024"):
        make_settings(embedding_dimension=768)


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"chunk_target_tokens": 100, "chunk_overlap_tokens": 100}, "CHUNK_OVERLAP_TOKENS"),
        ({"chunk_target_tokens": 100, "chunk_min_tokens": 100}, "CHUNK_MIN_TOKENS"),
        ({"chunk_target_tokens": 1000, "embedding_max_seq_length": 1024}, "truncated"),
        ({"worker_heartbeat_seconds": 60, "worker_lease_seconds": 30}, "HEARTBEAT"),
    ],
)
def test_inconsistent_ingestion_settings_are_rejected(
    make_settings: Callable[..., Settings], overrides: dict[str, object], message: str
) -> None:
    with pytest.raises(ValidationError, match=message):
        make_settings(**overrides)
