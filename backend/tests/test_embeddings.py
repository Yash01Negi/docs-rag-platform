import math
from collections.abc import Callable, Sequence
from pathlib import Path

import pytest

from app.core.config import Settings
from app.embeddings import create_embedding_provider
from app.embeddings.base import EmbeddingModelInfo, EmbeddingProvider, validate_embeddings
from app.embeddings.bge_m3 import BgeM3EmbeddingProvider, resolved_revision
from app.embeddings.fake import FAKE_MODEL_NAME, FakeEmbeddingProvider
from app.ingestion.errors import ErrorCode, IngestionError

DIM = 1024


def _unit(dimension: int = DIM, index: int = 0) -> list[float]:
    vector = [0.0] * dimension
    vector[index] = 1.0
    return vector


class StubProvider(EmbeddingProvider):
    """Returns whatever the test tells it to, to exercise the shared validation."""

    def __init__(self, output: Callable[[Sequence[str]], list[list[float]]]) -> None:
        super().__init__(dimension=DIM, batch_size=2)
        self.output = output
        self.batches: list[list[str]] = []

    def load(self) -> EmbeddingModelInfo:
        return EmbeddingModelInfo("stub", "1", DIM)

    def count_tokens(self, text: str) -> int:
        return len(text)

    @property
    def max_input_tokens(self) -> int:
        return 20

    def _embed_batch(self, texts: Sequence[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return self.output(texts)


def test_fake_provider_is_deterministic_normalized_and_1024_dimensional() -> None:
    provider = FakeEmbeddingProvider(DIM)
    first = provider.embed(["東京", "Tokyo"])
    second = provider.embed(["東京", "Tokyo"])

    assert first == second
    assert first[0] != first[1]
    for vector in first:
        assert len(vector) == DIM
        assert math.isclose(math.sqrt(sum(v * v for v in vector)), 1.0, abs_tol=1e-9)
    assert provider.load().name == FAKE_MODEL_NAME


def test_embeddings_are_computed_in_configured_batches() -> None:
    provider = FakeEmbeddingProvider(DIM, batch_size=3)
    vectors = provider.embed([f"text {i}" for i in range(7)])
    assert len(vectors) == 7
    assert provider.calls == [3, 3, 1]


def test_valid_output_passes_through_the_interface() -> None:
    provider = StubProvider(lambda texts: [_unit(index=i) for i, _ in enumerate(texts)])
    vectors = provider.embed(["a", "b", "c"])
    assert len(vectors) == 3
    assert provider.batches == [["a", "b"], ["c"]]


@pytest.mark.parametrize(
    ("output", "message"),
    [
        (lambda texts: [_unit(DIM - 1) for _ in texts], "1023 dimensions"),
        (lambda texts: [_unit(DIM + 1) for _ in texts], "1025 dimensions"),
        (lambda texts: [[math.nan] * DIM for _ in texts], "NaN or infinity"),
        (lambda texts: [[math.inf] + [0.0] * (DIM - 1) for _ in texts], "NaN or infinity"),
        (lambda texts: [[1.0] * DIM for _ in texts], "not normalised"),
        (lambda texts: [[0.0] * DIM for _ in texts], "not normalised"),
        (lambda texts: [_unit()], "Expected 2 embeddings"),
    ],
)
def test_invalid_vectors_are_rejected(
    output: Callable[[Sequence[str]], list[list[float]]], message: str
) -> None:
    provider = StubProvider(output)
    with pytest.raises(IngestionError, match=message) as exc:
        provider.embed(["a", "b"])
    assert exc.value.code is ErrorCode.INVALID_EMBEDDING


def test_inference_exceptions_become_retryable_errors() -> None:
    def explode(texts: Sequence[str]) -> list[list[float]]:
        raise RuntimeError("out of memory")

    with pytest.raises(IngestionError) as exc:
        StubProvider(explode).embed(["a"])
    assert exc.value.code is ErrorCode.EMBEDDING_FAILED
    assert exc.value.retryable is True
    assert "out of memory" not in exc.value.message  # internals are not exposed


def test_over_long_inputs_are_rejected_not_truncated() -> None:
    provider = StubProvider(lambda texts: [_unit() for _ in texts])
    with pytest.raises(IngestionError) as exc:
        provider.embed(["short", "x" * 21])
    assert exc.value.code is ErrorCode.INPUT_TOO_LONG
    assert provider.batches == []  # nothing was embedded


def test_validate_embeddings_accepts_float_like_values() -> None:
    assert validate_embeddings([tuple(_unit())], 1, DIM)[0][0] == 1.0


def test_factory_returns_fake_provider_in_test_environment(
    make_settings: Callable[..., Settings],
) -> None:
    provider = create_embedding_provider(make_settings(embedding_batch_size=5))
    assert isinstance(provider, FakeEmbeddingProvider)
    assert provider.batch_size == 5


def test_factory_returns_bge_m3_provider_without_loading_it(
    make_settings: Callable[..., Settings],
) -> None:
    settings = make_settings(
        embedding_provider="bge-m3",
        embedding_model_revision="0123abcd",
        embedding_device="cpu",
        embedding_max_seq_length=512,
    )
    provider = create_embedding_provider(settings)
    assert isinstance(provider, BgeM3EmbeddingProvider)
    assert provider.model_name == "BAAI/bge-m3"
    assert provider.revision == "0123abcd"
    assert provider.max_input_tokens == 510
    assert provider.cache_dir == settings.model_cache_dir


def test_bge_m3_reports_a_clear_error_when_the_model_is_unavailable(
    make_settings: Callable[..., Settings], monkeypatch: pytest.MonkeyPatch
) -> None:
    provider = create_embedding_provider(
        make_settings(embedding_provider="bge-m3", embedding_local_files_only=True)
    )
    # Either the optional libraries are missing (normal CI) or the model is not cached.
    with pytest.raises(IngestionError) as exc:
        provider.embed(["hello"])
    assert exc.value.code is ErrorCode.EMBEDDING_MODEL_UNAVAILABLE
    assert "random" not in exc.value.message


SHA = "5617a9f61b028005a4858fdac845db406aefb181"


@pytest.mark.parametrize("requested", [SHA, "main"])
def test_recorded_revision_is_the_snapshot_commit(requested: str) -> None:
    snapshot = Path("/models/models--BAAI--bge-m3/snapshots") / SHA
    assert resolved_revision(requested, snapshot) == SHA


@pytest.mark.parametrize(
    "snapshot_name",
    ["0" * 40, "not-a-commit"],
    ids=["other-commit", "not-a-commit"],
)
def test_snapshot_that_does_not_match_the_pinned_revision_is_refused(snapshot_name: str) -> None:
    with pytest.raises(IngestionError) as exc:
        resolved_revision(SHA, Path("/models/snapshots") / snapshot_name)
    assert exc.value.code is ErrorCode.EMBEDDING_MODEL_UNAVAILABLE
