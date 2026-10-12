"""Application settings, loaded from environment variables (and an optional .env file)."""

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Repository root (docs-rag-platform/). Used so a single root-level .env file works when the
# backend is started from either the repository root or the backend/ directory.
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Dense output size of BAAI/bge-m3, and the size of the chunks.embedding vector column.
EMBEDDING_DIMENSION = 1024


class Environment(StrEnum):
    DEVELOPMENT = "development"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    """Runtime configuration.

    Every value can be set through an environment variable of the same name in upper case,
    e.g. ``DATABASE_URL`` or ``CORS_ALLOWED_ORIGINS``. There are deliberately no defaults
    for credentials: ``DATABASE_URL`` must always be provided.
    """

    model_config = SettingsConfigDict(
        env_file=(_REPO_ROOT / ".env", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    app_name: str = "YashRAG API"
    environment: Environment = Environment.DEVELOPMENT
    log_level: str = "INFO"

    database_url: SecretStr
    database_connect_timeout_seconds: float = Field(default=3.0, gt=0, le=30)
    readiness_timeout_seconds: float = Field(default=5.0, gt=0, le=60)

    # Comma-separated list in the environment, e.g. "http://localhost:5173,https://app.example.com"
    cors_allowed_origins: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- Document storage and uploads ---
    # Original files are stored here under generated keys; never under user-supplied names.
    storage_dir: Path = Path("data/uploads")
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, gt=0, le=1024 * 1024 * 1024)

    # --- Embeddings ---
    # "bge-m3" is the production provider. "fake" is a deterministic test double and is only
    # accepted when ENVIRONMENT=test.
    embedding_provider: Literal["bge-m3", "fake"] = "bge-m3"
    embedding_model_name: str = "BAAI/bge-m3"
    # A branch, tag or commit SHA on the Hugging Face Hub. Pin a commit SHA for
    # reproducibility; the resolved commit is recorded on every indexed document either way.
    embedding_model_revision: str = "main"
    embedding_dimension: int = EMBEDDING_DIMENSION
    embedding_device: Literal["auto", "cpu", "mps", "cuda"] = "auto"
    embedding_batch_size: int = Field(default=16, ge=1, le=256)
    # Longest input the model is asked to encode. Inputs longer than this are rejected rather
    # than truncated. BGE-M3 supports up to 8192 tokens; a lower value bounds memory use.
    embedding_max_seq_length: int = Field(default=1024, ge=16, le=8192)
    model_cache_dir: Path = Path("data/models")
    # When true, never contact the Hugging Face Hub; the model must already be in the cache.
    embedding_local_files_only: bool = False

    # --- Chunking (sizes are in embedding-model tokens) ---
    chunk_target_tokens: int = Field(default=400, ge=32, le=4096)
    chunk_overlap_tokens: int = Field(default=60, ge=0, le=1024)
    chunk_min_tokens: int = Field(default=40, ge=0, le=1024)

    # --- Ingestion worker ---
    worker_concurrency: int = Field(default=1, ge=1, le=16)
    worker_poll_interval_seconds: float = Field(default=2.0, gt=0, le=60)
    worker_lease_seconds: float = Field(default=120.0, ge=5, le=3600)
    worker_heartbeat_seconds: float = Field(default=15.0, gt=0, le=600)
    worker_max_attempts: int = Field(default=3, ge=1, le=20)
    worker_retry_backoff_seconds: float = Field(default=30.0, ge=0, le=3600)
    worker_shutdown_grace_seconds: float = Field(default=30.0, ge=0, le=600)
    worker_heartbeat_file: Path | None = None
    worker_preload_model: bool = False

    @field_validator("log_level")
    @classmethod
    def _validate_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError(f"unsupported log level: {value!r}")
        return level

    @field_validator("database_url")
    @classmethod
    def _validate_database_url(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().startswith("postgresql+psycopg://"):
            raise ValueError("DATABASE_URL must use the 'postgresql+psycopg://' scheme")
        return value

    @field_validator("cors_allowed_origins", mode="before")
    @classmethod
    def _parse_cors_origins(cls, value: object) -> object:
        if isinstance(value, str):
            value = [origin.strip() for origin in value.split(",") if origin.strip()]
        if isinstance(value, list):
            for origin in value:
                if origin == "*":
                    raise ValueError(
                        "wildcard CORS origins are not allowed; list origins explicitly"
                    )
                if not origin.startswith(("http://", "https://")):
                    raise ValueError(f"CORS origin must start with http:// or https://: {origin!r}")
            return [origin.rstrip("/") for origin in value]
        return value

    @field_validator("embedding_dimension")
    @classmethod
    def _validate_embedding_dimension(cls, value: int) -> int:
        # The chunks.embedding column is vector(1024); changing it needs a migration.
        if value != EMBEDDING_DIMENSION:
            raise ValueError(
                f"embedding_dimension must be {EMBEDDING_DIMENSION} to match the database schema"
            )
        return value

    @model_validator(mode="after")
    def _validate_cross_field(self) -> "Settings":
        if self.embedding_provider == "fake" and self.environment is not Environment.TEST:
            raise ValueError("EMBEDDING_PROVIDER=fake is only allowed when ENVIRONMENT=test")
        if self.chunk_overlap_tokens >= self.chunk_target_tokens:
            raise ValueError("CHUNK_OVERLAP_TOKENS must be smaller than CHUNK_TARGET_TOKENS")
        if self.chunk_min_tokens >= self.chunk_target_tokens:
            raise ValueError("CHUNK_MIN_TOKENS must be smaller than CHUNK_TARGET_TOKENS")
        # The model adds two special tokens (<s> and </s>) to every input.
        if self.chunk_max_tokens + 2 > self.embedding_max_seq_length:
            raise ValueError(
                "CHUNK_TARGET_TOKENS + CHUNK_MIN_TOKENS + 2 must not exceed "
                "EMBEDDING_MAX_SEQ_LENGTH, or chunks would be truncated by the model"
            )
        if self.worker_heartbeat_seconds >= self.worker_lease_seconds:
            raise ValueError("WORKER_HEARTBEAT_SECONDS must be smaller than WORKER_LEASE_SECONDS")
        return self

    @property
    def chunk_max_tokens(self) -> int:
        """Hard upper bound for a chunk: the target plus room to absorb a tiny trailing chunk."""
        return self.chunk_target_tokens + self.chunk_min_tokens


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached after the first call)."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
