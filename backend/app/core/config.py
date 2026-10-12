"""Application settings, loaded from environment variables (and an optional .env file)."""

from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Repository root (docs-rag-platform/). Used so a single root-level .env file works when the
# backend is started from either the repository root or the backend/ directory.
_REPO_ROOT = Path(__file__).resolve().parents[3]


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


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached after the first call)."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
