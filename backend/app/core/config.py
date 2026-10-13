"""Application settings, loaded from environment variables (and an optional .env file)."""

import re
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# Repository root (docs-rag-platform/). Used so a single root-level .env file works when the
# backend is started from either the repository root or the backend/ directory.
_REPO_ROOT = Path(__file__).resolve().parents[3]

# Dense output size of BAAI/bge-m3, and the size of the chunks.embedding vector column.
EMBEDDING_DIMENSION = 1024
# The BAAI/bge-m3 commit on the Hugging Face Hub that Phase 2 was verified with.
BGE_M3_REVISION = "5617a9f61b028005a4858fdac845db406aefb181"


# Model IDs as Claude, Gemini and Ollama spell them ("claude-opus-5-5", "gemini-3.8-flash",
# "qwen3:8b", "library/model:tag"). Anything else is rejected at startup.
_MODEL_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$")
MAX_MODELS_PER_PROVIDER = 20


def _has_secret(value: SecretStr | None) -> bool:
    return value is not None and bool(value.get_secret_value().strip())


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

    # --- LLM providers and models (Phase 4 chat, Phase 4.1 multi-model) ---
    # The browser picks a model from the catalog built from these settings (GET
    # /chat/models); it can never name a provider, model or URL that is not listed here.
    # LLM_PROVIDER and LLM_MODEL are the default used when a request names no model. A
    # provider without credentials (or, for Ollama, without a base URL and models) is listed
    # as not configured; without any configured provider the chat endpoints answer 503.
    llm_provider: Literal["anthropic", "gemini", "ollama"] = "anthropic"
    # Unset means the first model in the default provider's list.
    llm_model: str | None = Field(default=None, min_length=1, max_length=128)

    # Anthropic Claude (paid API). Read by this application only and sent only to the
    # provider; never logged or returned.
    anthropic_api_key: SecretStr | None = None
    # Comma-separated model IDs offered in the model selector. The defaults are the current
    # Claude models listed in Anthropic's model overview on 2026-10-13; the server also asks
    # the Models API whether each one is available to the configured key.
    anthropic_models: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]
    )

    # Google Gemini, through the official Google Gen AI SDK (Gemini Developer API).
    gemini_api_key: SecretStr | None = None
    # The defaults are the stable Flash and Flash-Lite models listed in the Gemini API model
    # documentation on 2026-10-13; availability is checked against the API's model list.
    gemini_models: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["gemini-3.8-flash", "gemini-3.5-flash-lite"]
    )
    # Which Gemini API tier the key's project is on. It only labels the models ("free tier"
    # or "paid"); Google decides the tier from the project's billing. On the free tier,
    # Google may use prompts and answers to improve its products (docs/llm-providers.md).
    gemini_tier: Literal["free", "paid"] = "free"
    # Gemini 3 thinking level; unset uses each model's default. Thoughts are never shown or
    # stored, but they count as (billed) output tokens.
    gemini_thinking_level: Literal["minimal", "low", "medium", "high"] | None = None

    # Ollama (local models over its HTTP API). Both a base URL and at least one model are
    # needed. From Docker Desktop on macOS use http://host.docker.internal:11434; for the
    # API running directly on the host, http://localhost:11434.
    ollama_base_url: str | None = None
    # Comma-separated allowlist of local model names, e.g. "qwen3:8b,gemma3:12b". Only
    # models that are also installed (`ollama pull`) are offered as available.
    ollama_models: Annotated[list[str], NoDecode] = Field(default_factory=list)
    # Context window requested from Ollama. Ollama's own default (4096 tokens) is smaller
    # than the chat prompt can be, and Ollama silently drops the start of a longer prompt.
    ollama_num_ctx: int = Field(default=16384, ge=2048, le=1048576)
    # Ollama's "think" option: unset leaves each model's default. Thinking text is dropped.
    ollama_think: bool | None = None
    # How long Ollama keeps the model loaded after a request, e.g. "5m" (unset: its default).
    ollama_keep_alive: str | None = Field(default=None, pattern=r"^(-1|0|\d+(\.\d+)?(ms|s|m|h))$")
    # Ollama "cloud" models run on ollama.com, not on this machine; they are refused unless
    # this is true, so a "local" choice never sends documents elsewhere.
    ollama_allow_remote_models: bool = False

    # How long a model availability check (Models API, Ollama's model list) is reused.
    llm_catalog_ttl_seconds: float = Field(default=300.0, ge=0, le=86400)
    # Timeout for one availability check.
    llm_catalog_check_timeout_seconds: float = Field(default=5.0, gt=0, le=60)

    llm_max_output_tokens: int = Field(default=2048, ge=64, le=64000)
    # Sampling temperature for Claude and Gemini. Unset by default: current Claude models
    # (Claude Opus 5.5 included) reject the parameter, so set it only for a model that
    # accepts it. Ollama models use their own default.
    llm_temperature: float | None = Field(default=None, ge=0, le=1)
    # Claude reasoning effort (output_config.effort). Unset uses the model's default.
    llm_effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    # Claude server-side fallback when the requested model declines a request on policy
    # grounds ("default" lets the API choose the substitute Claude model; "none" turns it
    # off). It never switches to another provider.
    llm_fallbacks: Literal["default", "none"] = "default"
    # Connect timeout, and the longest wait for the next chunk of a streamed response.
    llm_connect_timeout_seconds: float = Field(default=10.0, gt=0, le=120)
    llm_read_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    # Upper bound for one whole answer, including retries before the first token.
    llm_request_timeout_seconds: float = Field(default=180.0, gt=0, le=1800)
    # Retries for connection errors, 408, 409, 429 and 5xx before any output streams (the
    # SDKs' own retries for Claude and Gemini, the application's for Ollama).
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

    @field_validator("anthropic_models", "gemini_models", "ollama_models", mode="before")
    @classmethod
    def _parse_models(cls, value: object) -> object:
        if isinstance(value, str):
            value = [model.strip() for model in value.split(",") if model.strip()]
        if isinstance(value, list):
            models: list[str] = []
            for model in value:
                if not isinstance(model, str) or not _MODEL_ID.match(model):
                    raise ValueError(f"invalid model ID: {model!r}")
                if model not in models:
                    models.append(model)
            if len(models) > MAX_MODELS_PER_PROVIDER:
                raise ValueError(f"at most {MAX_MODELS_PER_PROVIDER} models per provider")
            return models
        return value

    @field_validator("ollama_base_url")
    @classmethod
    def _validate_ollama_base_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        parsed = urlsplit(value.strip())
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("OLLAMA_BASE_URL must be an http:// or https:// URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("OLLAMA_BASE_URL must not contain credentials, a query or a fragment")
        return value.strip().rstrip("/")

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
        if self.llm_model is not None and not _MODEL_ID.match(self.llm_model):
            raise ValueError(f"invalid LLM_MODEL: {self.llm_model!r}")
        if self.worker_heartbeat_seconds >= self.worker_lease_seconds:
            raise ValueError("WORKER_HEARTBEAT_SECONDS must be smaller than WORKER_LEASE_SECONDS")
        return self

    @property
    def llm_configured(self) -> bool:
        """True when the Anthropic API key is set (kept for Phase 4 callers)."""
        return _has_secret(self.anthropic_api_key)

    @property
    def gemini_configured(self) -> bool:
        return _has_secret(self.gemini_api_key)

    @property
    def ollama_configured(self) -> bool:
        return self.ollama_base_url is not None and bool(self.ollama_models)

    def provider_models(self, provider: str) -> list[str]:
        """The models offered for a provider, the default model included."""
        models = {
            "anthropic": self.anthropic_models,
            "gemini": self.gemini_models,
            "ollama": self.ollama_models,
        }[provider]
        if provider == self.llm_provider and self.llm_model and self.llm_model not in models:
            # A model named only in LLM_MODEL stays selectable, as in Phase 4.
            return [self.llm_model, *models]
        return list(models)

    def first_model(self, provider: str) -> str:
        """LLM_MODEL for the default provider, otherwise the provider's first model."""
        if provider == self.llm_provider and self.llm_model:
            return self.llm_model
        models = self.provider_models(provider)
        if not models:
            raise ValueError(f"no models are configured for {provider}")
        return models[0]

    @property
    def default_model(self) -> str | None:
        """LLM_MODEL, or the first model listed for the default provider."""
        if self.llm_model:
            return self.llm_model
        models = self.provider_models(self.llm_provider)
        return models[0] if models else None

    @property
    def chunk_max_tokens(self) -> int:
        """Hard upper bound for a chunk: the target plus room to absorb a tiny trailing chunk."""
        return self.chunk_target_tokens + self.chunk_min_tokens


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings instance (cached after the first call)."""
    return Settings()  # type: ignore[call-arg]  # values come from the environment
