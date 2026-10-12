"""Embedding provider interface.

Ingestion depends only on :class:`EmbeddingProvider`. The production implementation runs
BAAI/bge-m3 locally (:mod:`app.embeddings.bge_m3`); a remote API-backed provider can be
added later by implementing the same three methods. Every provider's output goes through
:func:`validate_embeddings`, so an invalid vector can never reach the database.
"""

import math
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass

from app.ingestion.errors import ErrorCode, IngestionError

# Normalised vectors must have unit length within this tolerance.
_NORM_TOLERANCE = 1e-3


@dataclass(frozen=True)
class EmbeddingModelInfo:
    name: str
    revision: str | None
    dimension: int


class EmbeddingProvider(ABC):
    """Produces L2-normalised dense vectors of a fixed dimension."""

    def __init__(self, dimension: int, batch_size: int) -> None:
        self.dimension = dimension
        self.batch_size = batch_size

    @abstractmethod
    def load(self) -> EmbeddingModelInfo:
        """Load the model (downloading it on first use if allowed). Idempotent."""

    @abstractmethod
    def count_tokens(self, text: str) -> int:
        """Number of model tokens in ``text``, excluding special tokens."""

    @property
    @abstractmethod
    def max_input_tokens(self) -> int:
        """Longest input (excluding special tokens) that is embedded without truncation."""

    @abstractmethod
    def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed one batch. Implementations must not truncate or pad the batch."""

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Embed texts in batches of ``batch_size`` and validate every vector."""
        info = self.load()
        for index, text in enumerate(texts):
            tokens = self.count_tokens(text)
            if tokens > self.max_input_tokens:
                raise IngestionError(
                    ErrorCode.INPUT_TOO_LONG,
                    f"Input {index} has {tokens} tokens; the limit is {self.max_input_tokens}.",
                )
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.batch_size):
            batch = texts[start : start + self.batch_size]
            try:
                result = self._embed_batch(batch)
            except IngestionError:
                raise
            except Exception as exc:
                raise IngestionError(
                    ErrorCode.EMBEDDING_FAILED,
                    f"Embedding inference failed ({type(exc).__name__}).",
                    retryable=True,
                ) from exc
            vectors.extend(validate_embeddings(result, len(batch), info.dimension))
        return vectors


def validate_embeddings(
    vectors: Sequence[Sequence[float]], expected_count: int, dimension: int
) -> list[list[float]]:
    """Reject wrong counts, wrong dimensions, non-finite values and non-normalised vectors."""
    if len(vectors) != expected_count:
        raise IngestionError(
            ErrorCode.INVALID_EMBEDDING,
            f"Expected {expected_count} embeddings, received {len(vectors)}.",
        )
    validated: list[list[float]] = []
    for index, vector in enumerate(vectors):
        values = [float(value) for value in vector]
        if len(values) != dimension:
            raise IngestionError(
                ErrorCode.INVALID_EMBEDDING,
                f"Embedding {index} has {len(values)} dimensions; expected {dimension}.",
            )
        if not all(math.isfinite(value) for value in values):
            raise IngestionError(
                ErrorCode.INVALID_EMBEDDING, f"Embedding {index} contains NaN or infinity."
            )
        norm = math.sqrt(math.fsum(value * value for value in values))
        if abs(norm - 1.0) > _NORM_TOLERANCE:
            raise IngestionError(
                ErrorCode.INVALID_EMBEDDING,
                f"Embedding {index} is not normalised (L2 norm {norm:.4f}).",
            )
        validated.append(values)
    return validated
