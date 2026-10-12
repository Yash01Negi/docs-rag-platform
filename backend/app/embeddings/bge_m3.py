"""Local BAAI/bge-m3 dense embeddings with sentence-transformers and PyTorch.

Requires the optional ``embeddings`` dependencies (``uv sync --extra embeddings``); the API
process never imports this module.

The model is assembled explicitly as Transformer -> CLS pooling -> L2 normalisation, which
is how BGE-M3 defines its dense vector (the upstream FlagEmbedding ``BGEM3FlagModel`` uses
``pooling_method="cls"`` and ``normalize_embeddings=True``). Building it explicitly avoids
depending on pooling configuration files in the model repository.

Weights are fetched with :func:`huggingface_hub.snapshot_download` into ``cache_dir`` on
first use (or ahead of time with ``python -m app.worker prepare-model``). Only the files
needed for dense inference are downloaded; the ONNX export and the sparse/ColBERT heads are
skipped. The resolved commit SHA is reported as the model revision.
"""

import logging
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.embeddings.base import EmbeddingModelInfo, EmbeddingProvider
from app.ingestion.errors import ErrorCode, IngestionError

logger = logging.getLogger(__name__)

# Files in the BAAI/bge-m3 repository that dense inference does not need.
_IGNORE_PATTERNS = [
    "onnx/*",
    "*.onnx",
    "*.onnx_data",
    "colbert_linear.pt",
    "sparse_linear.pt",
    "imgs/*",
    "*.jpg",
    "*.png",
]


def _require_model_libraries() -> None:
    try:
        import huggingface_hub  # noqa: F401
        import sentence_transformers  # noqa: F401
        import torch  # noqa: F401
    except ImportError as exc:
        raise IngestionError(
            ErrorCode.EMBEDDING_MODEL_UNAVAILABLE,
            "The embedding libraries are not installed; install them with "
            "'uv sync --extra embeddings'.",
        ) from exc


def resolve_device(requested: str) -> str:
    """Pick the inference device: MPS on Apple Silicon when available, otherwise CPU."""
    import torch

    if requested == "auto":
        return "mps" if torch.backends.mps.is_available() else "cpu"
    if requested == "mps" and not torch.backends.mps.is_available():
        raise IngestionError(
            ErrorCode.EMBEDDING_MODEL_UNAVAILABLE,
            "EMBEDDING_DEVICE=mps was requested but MPS is not available on this machine.",
        )
    if requested == "cuda" and not torch.cuda.is_available():
        raise IngestionError(
            ErrorCode.EMBEDDING_MODEL_UNAVAILABLE,
            "EMBEDDING_DEVICE=cuda was requested but CUDA is not available on this machine.",
        )
    return requested


class BgeM3EmbeddingProvider(EmbeddingProvider):
    def __init__(
        self,
        *,
        model_name: str,
        revision: str,
        dimension: int,
        batch_size: int,
        device: str,
        max_seq_length: int,
        cache_dir: Path,
        local_files_only: bool,
    ) -> None:
        super().__init__(dimension, batch_size)
        self.model_name = model_name
        self.revision = revision
        self.requested_device = device
        self.max_seq_length = max_seq_length
        self.cache_dir = cache_dir
        self.local_files_only = local_files_only
        self._model: Any = None
        self._info: EmbeddingModelInfo | None = None
        self._device: str | None = None
        # One model instance per process, shared by all jobs; loading and inference are
        # serialised because a PyTorch model is not safe to use from several threads at once.
        self._lock = threading.Lock()

    @property
    def device(self) -> str | None:
        return self._device

    @property
    def max_input_tokens(self) -> int:
        return self.max_seq_length - 2  # <s> and </s>

    def download(self) -> Path:
        """Download (or locate in the cache) the model snapshot; returns its directory."""
        from huggingface_hub import snapshot_download

        try:
            path = snapshot_download(
                repo_id=self.model_name,
                revision=self.revision,
                cache_dir=str(self.cache_dir),
                local_files_only=self.local_files_only,
                ignore_patterns=_IGNORE_PATTERNS,
            )
        except Exception as exc:
            hint = (
                "it is not in the local cache (EMBEDDING_LOCAL_FILES_ONLY=true)"
                if self.local_files_only
                else "the download failed"
            )
            raise IngestionError(
                ErrorCode.EMBEDDING_MODEL_UNAVAILABLE,
                f"Embedding model {self.model_name}@{self.revision} is unavailable: {hint} "
                f"({type(exc).__name__}).",
                retryable=True,
            ) from exc
        return Path(path)

    def load(self) -> EmbeddingModelInfo:
        if self._info is not None:
            return self._info
        with self._lock:
            if self._info is not None:
                return self._info
            _require_model_libraries()
            snapshot = self.download()
            device = resolve_device(self.requested_device)
            try:
                from sentence_transformers import SentenceTransformer, models

                transformer = models.Transformer(str(snapshot), max_seq_length=self.max_seq_length)
                pooling = models.Pooling(transformer.get_embedding_dimension(), pooling_mode="cls")
                model = SentenceTransformer(
                    modules=[transformer, pooling, models.Normalize()], device=device
                )
                model.eval()
            except Exception as exc:
                raise IngestionError(
                    ErrorCode.EMBEDDING_MODEL_UNAVAILABLE,
                    f"Embedding model could not be loaded ({type(exc).__name__}).",
                    retryable=True,
                ) from exc

            actual_dimension = model.get_embedding_dimension()
            if actual_dimension != self.dimension:
                raise IngestionError(
                    ErrorCode.INVALID_EMBEDDING,
                    f"Model {self.model_name} produces {actual_dimension}-dimensional vectors; "
                    f"the database expects {self.dimension}.",
                )
            self._model = model
            self._device = device
            # snapshot_download returns .../snapshots/<commit sha>
            self._info = EmbeddingModelInfo(self.model_name, snapshot.name, self.dimension)
            logger.info(
                "embedding_model_loaded",
                extra={
                    "model": self.model_name,
                    "revision": snapshot.name,
                    "device": device,
                    "max_seq_length": self.max_seq_length,
                },
            )
            return self._info

    def count_tokens(self, text: str) -> int:
        self.load()
        tokenizer = self._model.tokenizer
        return len(tokenizer(text, add_special_tokens=False, truncation=False)["input_ids"])

    def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        with self._lock:
            embeddings = self._model.encode(
                list(texts),
                batch_size=self.batch_size,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
        return embeddings.tolist()
