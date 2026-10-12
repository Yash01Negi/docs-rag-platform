"""Opt-in smoke test with the real BAAI/bge-m3 model.

Skipped unless RUN_MODEL_TESTS=1. Needs the optional dependencies and roughly 2.3 GB of
disk for the weights (downloaded into MODEL_CACHE_DIR on first run):

    uv sync --extra embeddings
    RUN_MODEL_TESTS=1 uv run pytest -m model -s

Never run in normal CI.
"""

import math
import os
from pathlib import Path

import pytest

from app.core.config import Settings

pytestmark = pytest.mark.model

if os.environ.get("RUN_MODEL_TESTS") != "1":
    pytest.skip("set RUN_MODEL_TESTS=1 to run the real BGE-M3 smoke test", allow_module_level=True)

pytest.importorskip("sentence_transformers", reason="install with: uv sync --extra embeddings")

from app.embeddings.bge_m3 import BgeM3EmbeddingProvider  # noqa: E402


@pytest.fixture(scope="module")
def provider() -> BgeM3EmbeddingProvider:
    settings = Settings(  # type: ignore[call-arg]
        database_url="postgresql+psycopg://unused:unused@127.0.0.1:1/unused",
        model_cache_dir=Path(os.environ.get("MODEL_CACHE_DIR", "data/models")),
    )
    return BgeM3EmbeddingProvider(
        model_name=settings.embedding_model_name,
        revision=settings.embedding_model_revision,
        dimension=settings.embedding_dimension,
        batch_size=settings.embedding_batch_size,
        device=settings.embedding_device,
        max_seq_length=settings.embedding_max_seq_length,
        cache_dir=settings.model_cache_dir,
        local_files_only=settings.embedding_local_files_only,
    )


def _norm(vector: list[float]) -> float:
    return math.sqrt(sum(v * v for v in vector))


@pytest.mark.parametrize(
    "text",
    [
        "YashRAG indexes English and Japanese documents.",
        "YashRAGは日本語と英語の文書を索引します。",
    ],
)
def test_real_model_produces_finite_normalized_1024_vectors(
    provider: BgeM3EmbeddingProvider, text: str
) -> None:
    [vector] = provider.embed([text])
    info = provider.load()
    print(f"model={info.name} revision={info.revision} device={provider.device}")

    assert len(vector) == 1024
    assert all(math.isfinite(v) for v in vector)
    assert math.isclose(_norm(vector), 1.0, abs_tol=1e-3)


def test_real_model_is_deterministic_within_tolerance(provider: BgeM3EmbeddingProvider) -> None:
    texts = ["Deterministic inference check.", "決定的な推論の確認です。"]
    first = provider.embed(texts)
    second = provider.embed(texts)
    for a, b in zip(first, second, strict=True):
        assert max(abs(x - y) for x, y in zip(a, b, strict=True)) < 1e-4


def test_translation_pair_is_closer_than_unrelated_text(provider: BgeM3EmbeddingProvider) -> None:
    english, japanese, unrelated = provider.embed(
        [
            "The cat is sleeping on the sofa.",
            "猫がソファで寝ています。",
            "Quarterly tax filing deadline.",
        ]
    )

    def cosine(a: list[float], b: list[float]) -> float:
        return sum(x * y for x, y in zip(a, b, strict=True))

    # A sanity check that the weights are real, not a benchmark claim.
    assert cosine(english, japanese) > cosine(english, unrelated)
