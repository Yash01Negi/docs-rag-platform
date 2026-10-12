"""Deterministic fake embeddings. FOR TESTS ONLY.

This provider exists so the ingestion pipeline can be tested in CI without downloading a
2 GB model. Its vectors are derived from a SHA-256 hash of the input text: the same text
always maps to the same unit vector, but the vectors carry **no semantic meaning**.

It can only be selected with ``EMBEDDING_PROVIDER=fake`` together with ``ENVIRONMENT=test``
(enforced by :class:`app.core.config.Settings`), and documents indexed with it record the
model name ``fake-deterministic`` so they can never be mistaken for real embeddings.
"""

import hashlib
import math
import random
import re
from collections.abc import Sequence

from app.embeddings.base import EmbeddingModelInfo, EmbeddingProvider

FAKE_MODEL_NAME = "fake-deterministic"
FAKE_MODEL_REVISION = "v1"

_WORD = re.compile(r"[A-Za-z0-9]+|\S")


def fake_token_count(text: str) -> int:
    """Approximate token count: one per CJK character, one per Latin word or symbol.

    Deterministic and dependency-free; real token counts come from the model's tokenizer.
    """
    return sum(1 if len(token) == 1 else _word_tokens(token) for token in _WORD.findall(text))


def _word_tokens(word: str) -> int:
    # Long words are split into several subword tokens by real tokenizers.
    return max(1, math.ceil(len(word) / 6))


class FakeEmbeddingProvider(EmbeddingProvider):
    def __init__(self, dimension: int, batch_size: int = 16, max_input_tokens: int = 8192) -> None:
        super().__init__(dimension, batch_size)
        self._max_input_tokens = max_input_tokens
        self.calls: list[int] = []  # batch sizes, for assertions in tests

    def load(self) -> EmbeddingModelInfo:
        return EmbeddingModelInfo(FAKE_MODEL_NAME, FAKE_MODEL_REVISION, self.dimension)

    def count_tokens(self, text: str) -> int:
        return fake_token_count(text)

    @property
    def max_input_tokens(self) -> int:
        return self._max_input_tokens

    def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        self.calls.append(len(texts))
        return [self._vector(text) for text in texts]

    def _vector(self, text: str) -> list[float]:
        seed = int.from_bytes(hashlib.sha256(text.encode("utf-8")).digest()[:8], "big")
        rng = random.Random(seed)  # noqa: S311 - not used for security
        values = [rng.gauss(0.0, 1.0) for _ in range(self.dimension)]
        norm = math.sqrt(math.fsum(v * v for v in values))
        return [v / norm for v in values]
