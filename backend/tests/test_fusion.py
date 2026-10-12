"""Reciprocal Rank Fusion and snippet selection (no database needed)."""

import uuid

import pytest

from app.search.retrieval import Candidate, reciprocal_rank_fusion
from app.search.snippets import make_snippet

A, B, C, D = (uuid.UUID(int=n) for n in (1, 2, 3, 4))


def _ranked(*ids: uuid.UUID) -> list[Candidate]:
    return [Candidate(chunk_id, rank, 1.0 / rank) for rank, chunk_id in enumerate(ids, start=1)]


def test_rrf_scores_are_sums_of_reciprocal_ranks() -> None:
    fused = reciprocal_rank_fusion(
        {"semantic": _ranked(A, B, C), "keyword": _ranked(C, A)}, k=60, limit=10
    )
    scores = {entry.chunk_id: entry.score for entry in fused}
    assert scores[A] == pytest.approx(1 / 61 + 1 / 62)
    assert scores[C] == pytest.approx(1 / 63 + 1 / 61)
    assert scores[B] == pytest.approx(1 / 62)
    assert [entry.chunk_id for entry in fused] == [A, C, B]


def test_rrf_keeps_each_chunk_once_with_per_method_ranks() -> None:
    fused = reciprocal_rank_fusion({"semantic": _ranked(A, B), "keyword": _ranked(B)}, 60, 10)
    assert [entry.chunk_id for entry in fused] == [B, A]
    assert fused[0].ranks == {"semantic": 2, "keyword": 1}
    assert fused[1].ranks == {"semantic": 1}


def test_rrf_breaks_ties_by_best_rank_then_chunk_id() -> None:
    # B and C tie (1/61 + 1/63 each) and both have a first place, so the ID decides.
    fused = reciprocal_rank_fusion(
        {"semantic": _ranked(C, D, B), "keyword": _ranked(B, D, C)}, 60, 10
    )
    assert [entry.chunk_id for entry in fused] == [B, C, D]
    # Same input, other dict order: same output.
    again = reciprocal_rank_fusion(
        {"keyword": _ranked(B, D, C), "semantic": _ranked(C, D, B)}, 60, 10
    )
    assert [entry.chunk_id for entry in again] == [B, C, D]


def test_rrf_constant_changes_how_much_top_ranks_dominate() -> None:
    rankings = {"semantic": _ranked(A, B, C, D), "keyword": _ranked(D, C, B, A)}
    # With every chunk found by both methods at mirrored ranks, all totals are close; a
    # small k favours chunks with one very high rank.
    small = reciprocal_rank_fusion(rankings, 1, 10)
    assert [entry.chunk_id for entry in small][:2] == [A, D]


def test_rrf_limit_and_empty_inputs() -> None:
    assert reciprocal_rank_fusion({"semantic": [], "keyword": []}, 60, 5) == []
    fused = reciprocal_rank_fusion({"semantic": _ranked(A, B, C)}, 60, 2)
    assert [entry.chunk_id for entry in fused] == [A, B]


def test_rrf_rejects_a_chunk_listed_twice_by_one_method() -> None:
    with pytest.raises(ValueError, match="twice"):
        reciprocal_rank_fusion({"semantic": [Candidate(A, 1, 1.0), Candidate(A, 2, 0.5)]}, 60, 5)


def test_short_chunk_is_its_own_snippet() -> None:
    snippet = make_snippet("全文検索エンジン", ("検索",), 280)
    assert snippet.text == "全文検索エンジン" and snippet.start == 0
    assert [snippet.text[s:e] for s, e in snippet.highlights] == ["検索"]


def test_long_chunk_snippet_is_centred_on_matches() -> None:
    text = "Intro sentence. " * 40 + "The keyword appears here twice: keyword. " + "Tail. " * 40
    snippet = make_snippet(text, ("keyword",), 120)
    assert len(snippet.text) <= 120
    assert snippet.start > 0
    marked = [snippet.text[s:e] for s, e in snippet.highlights]
    assert marked == ["keyword", "keyword"]
    assert text[snippet.start : snippet.start + len(snippet.text)] == snippet.text


def test_snippet_without_matches_starts_at_the_beginning() -> None:
    snippet = make_snippet("word " * 200, (), 100)
    assert snippet.start == 0 and snippet.highlights == [] and len(snippet.text) == 100
