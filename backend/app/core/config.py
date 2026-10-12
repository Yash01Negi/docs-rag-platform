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
# The BAAI/bge-m3 commit on the Hugging Face Hub that Phase 2 was verified with.
BGE_M3_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"


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
    # A commit SHA (or branch or tag) on the Hugging Face Hub. The default is the BAAI/bge-m3
    # commit verified for Phase 2; the resolved commit is recorded on every indexed document.
    embedding_model_revision: str = Field(default=BGE_M3_REVISION, min_length=1)
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

    # --- Search ---
    # Mode used when a request does not name one. Semantic scored best on the evaluation set
    # (docs/evaluation/README.md); hybrid and keyword stay available per request.
    search_default_mode: Literal["semantic", "keyword", "hybrid"] = "semantic"
    search_default_top_k: int = Field(default=10, ge=1, le=100)
    search_max_top_k: int = Field(default=50, ge=1, le=200)
    search_max_query_chars: int = Field(default=1000, ge=10, le=10000)
    # Candidates taken from each method before Reciprocal Rank Fusion, and the RRF constant k
    # in 1 / (k + rank). 60 is the value from the original RRF paper (Cormack et al., 2009).
    search_fusion_depth: int = Field(default=50, ge=1, le=1000)
    search_rrf_k: int = Field(default=60, ge=1, le=1000)
    # Okapi BM25 parameters for keyword search (the common defaults).
    search_bm25_k1: float = Field(default=1.2, ge=0, le=10)
    search_bm25_b: float = Field(default=0.75, ge=0, le=1)
    search_snippet_chars: int = Field(default=280, ge=40, le=4000)
    # Load the query-embedding model when the API starts instead of on the first search.
    search_preload_model: bool = False
    # How long a search waits for the model to finish loading before hybrid search falls
    # back to keyword results (and semantic search answers 503). The load continues.
    search_model_load_wait_seconds: float = Field(default=20.0, ge=0, le=600)

    # --- LLM provider (Phase 4 chat) ---
    # Only "anthropic" is implemented. Without ANTHROPIC_API_KEY the chat endpoints answer
    # 503 llm_not_configured; conversations can still be listed, read, renamed and deleted.
    llm_provider: Literal["anthropic"] = "anthropic"
    # Read by this application only and sent only to the provider; never logged or returned.
    anthropic_api_key: SecretStr | None = None
    llm_model: str = Field(default="claude-opus-5-5", min_length=1, max_length=128)
    llm_max_output_tokens: int = Field(default=2048, ge=64, le=64000)
    # Sampling temperature. Unset by default: current Claude models (Claude Opus 5.5
    # included) reject the parameter, so set it only for a model that accepts it.
    llm_temperature: float | None = Field(default=None, ge=0, le=1)
    # Reasoning effort (output_config.effort). Unset uses the model's default.
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    # Server-side fallback when the requested model declines a request on policy grounds
    # ("default" lets the API choose the substitute model; "none" turns it off).
    llm_fallbacks: Literal["default", "none"] = "default"
    # Connect timeout, and the longest wait for the next chunk of a streamed response.
    llm_connect_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    llm_read_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    # Upper bound for one whole answer, including retries before the first token.
    llm_request_timeout_seconds: float = Field(default=180.0, gt=0, le=1800)
    # SDK retries for connection errors, 408, 409, 429 and 5xx before any output streams.
    llm_max_retries: int = Field(default=2, ge=0, le=10)

    # --- Chat ---
    chat_max_question_chars: int = Field(default=4000, ge=10, le=20000)
    chat_max_title_chars: int = Field(default=200, ge=10, le=500)
    # Retrieval for each question; the mode defaults to SEARCH_DEFAULT_MODE.
    chat_retrieval_mode: Literal["semantic", "keyword", "hybrid"] | None = None
    chat_retrieval_top_k: int = Field(default=8, ge=1, le=50)
    # Semantic candidates below this cosine similarity are not given to the model. Unset by
    # default because the threshold has not been calibrated (docs/evaluation/README.md).
    chat_min_similarity: float | None = Field(default=None, ge=-1, le=1)
    # Estimated-token budgets for the source passages and for earlier turns in the prompt.
    chat_context_token_budget: int = Field(default=8000, ge=500, le=150000)
    chat_history_token_budget: int = Field(default=3000, ge=0, le=100000)
    chat_history_max_messages: int = Field(default=10, ge=0, le=100)
    # A question shorter than this many characters is treated as a follow-up: the previous
    # question is added to the retrieval query (not to the prompt) so "and in Japan?" works.
    chat_followup_max_chars: int = Field(default=60, ge=0, le=1000)
    # Answers generated at the same time across all conversations (one per conversation).
    chat_max_concurrent_generations: int = Field(default=4, ge=1, le=100)
    # Characters of each cited passage stored with the answer, for display after the
    # source document is reindexed. Cleared when the document is deleted.
    chat_citation_excerpt_chars: int = Field(default=600, ge=0, le=4000)
    # SSE comment sent while waiting, so proxies do not close an idle connection.
    chat_heartbeat_seconds: float = Field(default=15.0, gt=0, le=300)

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
        if self.search_default_top_k > self.search_max_top_k:
            raise ValueError("SEARCH_DEFAULT_TOP_K must not exceed SEARCH_MAX_TOP_K")
        if self.chat_retrieval_top_k > self.search_max_top_k:
            raise ValueError("CHAT_RETRIEVAL_TOP_K must not exceed SEARCH_MAX_TOP_K")
        if self.worker_heartbeat_seconds >= self.worker_lease_seconds:
            raise ValueError("WORKER_HEARTBEAT_SECONDS must be smaller than WORKER_LEASE_SECONDS")
        return self

    @property
    def llm_configured(self) -> bool:
        key = self.anthropic_api_key
        return key is not None and bool(key.get_secret_value().strip())

    @property
    def chunk_max_tokens(self) -> int:
        """Hard upper bound for a chunk: the target plus room to absorb a tiny trailing chunk."""
        return self.chunk_target_tokens + self.chunk_min_tokens


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached after the first call)."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
