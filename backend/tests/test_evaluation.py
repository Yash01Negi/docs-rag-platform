"""The evaluation dataset, its metrics, and a keyword-mode run of the harness."""

import asyncio
import math
import shutil
from pathlib import Path

import pytest
from pydantic import SecretStr

from app.core.config import Settings
from app.search.service import SearchMode
from evaluation.dataset import DATASET_DIR, QUERY_TYPES, DatasetError, load_dataset
from evaluation.metrics import mean, percentile, query_metrics
from evaluation.run import EvaluationError, evaluate, render_report


def _set(*indexes: int) -> frozenset[int]:
    return frozenset(indexes)


# ------------------------------------------------------------------------------ metrics


def test_perfect_ranking_scores_one() -> None:
    values = query_metrics([_set(0), _set(1)], [2, 1], k=2)
    assert values.recall == values.precision == values.reciprocal_rank == 1.0
    assert values.ndcg == pytest.approx(1.0)


def test_nothing_relevant_retrieved_scores_zero() -> None:
    values = query_metrics([_set(), _set()], [2], k=5)
    assert values == type(values)(recall=0.0, precision=0.0, reciprocal_rank=0.0, ndcg=0.0)


def test_precision_divides_by_k_even_when_fewer_results_come_back() -> None:
    assert query_metrics([_set(0)], [2], k=5).precision == pytest.approx(0.2)


def test_reciprocal_rank_uses_the_first_relevant_chunk() -> None:
    assert query_metrics([_set(), _set(), _set(0)], [2], k=10).reciprocal_rank == pytest.approx(
        1 / 3
    )


def test_cutoff_ignores_chunks_below_k() -> None:
    values = query_metrics([_set(), _set(0)], [2], k=1)
    assert values.recall == 0.0 and values.reciprocal_rank == 0.0


def test_a_passage_found_twice_is_credited_once() -> None:
    # Two overlapping chunks contain passage 0; passage 1 is never found.
    values = query_metrics([_set(0), _set(0)], [2, 2], k=2)
    assert values.recall == pytest.approx(0.5)
    assert values.precision == pytest.approx(1.0)  # both chunks are relevant to read
    ideal = 3 / math.log2(2) + 3 / math.log2(3)
    assert values.ndcg == pytest.approx((3 / math.log2(2)) / ideal)


def test_ndcg_prefers_the_better_passage_first() -> None:
    best_first = query_metrics([_set(0), _set(1)], [2, 1], k=2).ndcg
    worst_first = query_metrics([_set(1), _set(0)], [2, 1], k=2).ndcg
    assert best_first == pytest.approx(1.0)
    assert worst_first < best_first


def test_one_chunk_holding_two_passages_counts_its_best_grade() -> None:
    values = query_metrics([_set(0, 1)], [1, 2], k=1)
    assert values.recall == 1.0
    assert values.ndcg == pytest.approx(1.0)  # ideal@1 is a single grade-2 passage


@pytest.mark.parametrize(
    ("ranking", "grades", "k"),
    [([_set(0)], [], 5), ([_set(0)], [2], 0), ([_set(3)], [2], 5), ([_set(0)], [0], 5)],
)
def test_metrics_reject_invalid_input(
    ranking: list[frozenset[int]], grades: list[int], k: int
) -> None:
    with pytest.raises(ValueError):
        query_metrics(ranking, grades, k)


def test_percentile_is_nearest_rank() -> None:
    values = [float(v) for v in range(1, 21)]  # 1..20
    assert percentile(values, 50) == 10.0
    assert percentile(values, 95) == 19.0
    assert percentile(values, 100) == 20.0
    assert percentile([7.0], 95) == 7.0
    with pytest.raises(ValueError):
        percentile([], 50)
    assert mean([1.0, 2.0]) == 1.5


# ------------------------------------------------------------------------------ dataset


def test_dataset_is_valid_and_covers_every_query_type() -> None:
    dataset = load_dataset()
    assert {t for query in dataset.queries for t in query.types} == set(QUERY_TYPES)
    assert {query.language for query in dataset.queries} == {"en", "ja", "mixed"}
    assert {name.split("-")[0] for name in dataset.documents} == {"en", "ja", "mixed"}
    # Every document is used, either as a relevant source or as a distractor in the notes.
    judged = {passage.doc for query in dataset.queries for passage in query.relevant}
    assert judged | {"en-office-guide.md", "en-snapkeep-release-notes.md"} == set(dataset.documents)


def _copy_dataset(tmp_path: Path) -> Path:
    target = tmp_path / "dataset"
    shutil.copytree(DATASET_DIR, target)
    return target


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('anchor = "daily per diem of 60', 'anchor = "daily per diem of 70', "not in"),
        (
            'doc = "en-travel-expenses.txt", anchor = "daily',
            'doc = "nope.txt", anchor = "daily',
            "unknown document",
        ),
        ('id = "en-02"', 'id = "en-01"', "duplicate"),
        ('types = ["no_relevant"]\nnotes', 'types = ["paraphrase"]\nnotes', "no_relevant"),
        ('language = "mixed"', 'language = "fr"', "language"),
    ],
)
def test_dataset_check_rejects_broken_judgments(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    directory = _copy_dataset(tmp_path)
    queries = directory / "queries.toml"
    source = queries.read_text(encoding="utf-8")
    assert old in source
    queries.write_text(source.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(DatasetError, match=message):
        load_dataset(directory)


# ------------------------------------------------------------------------------ harness


@pytest.mark.integration
def test_keyword_evaluation_runs_end_to_end(db_settings: Settings) -> None:
    result = asyncio.run(
        evaluate(db_settings, [SearchMode.KEYWORD], repeats=1, reset=True, allow_fake=False)
    )
    summary = result["summary"]["keyword"]
    dataset = load_dataset()
    judged = [query for query in dataset.queries if query.relevant]
    assert summary["by_language"]["all"]["queries"] == len(judged)
    assert summary["no_relevant"]["queries"] == len(dataset.queries) - len(judged)
    for values in summary["by_language"].values():
        assert all(0.0 <= values[name] <= 1.0 for name in values if name != "queries")
    # A regression floor, not a target: keyword search must find exact keyword matches.
    assert summary["by_type"]["exact_keyword"]["recall@10"] >= 0.9
    assert summary["by_type"]["mixed"]["recall@10"] >= 0.9
    assert result["metadata"]["chunks"] > len(dataset.documents)

    report = render_report(result)
    assert "| keyword | all |" in report
    assert "Keyword mode only" in report
    assert "Fake embeddings." not in report


@pytest.mark.integration
def test_harness_refuses_fake_semantic_numbers_unless_asked(db_settings: Settings) -> None:
    with pytest.raises(EvaluationError, match="meaningless"):
        asyncio.run(
            evaluate(db_settings, [SearchMode.HYBRID], repeats=1, reset=True, allow_fake=False)
        )
    result = asyncio.run(
        evaluate(db_settings, list(SearchMode), repeats=1, reset=True, allow_fake=True)
    )
    assert set(result["summary"]) == {"semantic", "keyword", "hybrid"}
    assert "Fake embeddings." in render_report(result)


@pytest.mark.integration
def test_harness_will_not_wipe_a_database_with_another_name(
    db_settings: Settings,
) -> None:
    url = db_settings.database_url.get_secret_value().rsplit("/", 1)[0] + "/postgres"
    settings = db_settings.model_copy(update={"database_url": SecretStr(url)})
    with pytest.raises(EvaluationError, match="refusing"):
        asyncio.run(evaluate(settings, [SearchMode.KEYWORD], 1, reset=True, allow_fake=False))
