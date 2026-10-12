"""Evaluate fusion settings offline from each method's own candidate lists.

Hybrid search fuses the top `fusion_depth` candidates of semantic and keyword search. The
sweep fetches both lists once per query (at the largest depth), then re-fuses them for every
combination of RRF k, depth and keyword weight, so trying a setting needs no new searches.

The keyword weight is an experiment: the application's RRF weighs both methods equally. A
weighted variant scores Σ w_method / (k + rank).
"""

import uuid
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

from evaluation.dataset import Dataset
from evaluation.metrics import mean, query_metrics

RRF_KS = (1, 5, 10, 20, 40, 60, 100)
DEPTHS = (10, 20, 50)
KEYWORD_WEIGHTS = (1.0, 0.5, 0.25)
CUTOFF = 10


def weighted_rrf(
    rankings: Mapping[str, Sequence[uuid.UUID]],
    k: int,
    weights: Mapping[str, float],
    limit: int,
) -> list[uuid.UUID]:
    """RRF with per-method weights; ties broken like the application (best rank, then ID)."""
    scores: dict[uuid.UUID, float] = {}
    best: dict[uuid.UUID, int] = {}
    for method, ids in rankings.items():
        for rank, chunk_id in enumerate(ids, start=1):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + weights[method] / (k + rank)
            best[chunk_id] = min(best.get(chunk_id, rank), rank)
    ordered = sorted(scores, key=lambda c: (-scores[c], best[c], str(c)))
    return ordered[:limit]


@dataclass(frozen=True)
class SweepRow:
    label: str
    rrf_k: int | None
    depth: int | None
    keyword_weight: float | None
    ndcg: float
    recall: float
    mrr: float
    ndcg_by_language: dict[str, float]


def _score(
    dataset: Dataset,
    ranked: Mapping[str, Sequence[uuid.UUID]],
    passages_of: Mapping[tuple[str, uuid.UUID], frozenset[int]],
) -> tuple[float, float, float, dict[str, float]]:
    rows: list[tuple[str, float, float, float]] = []
    for query in dataset.queries:
        if not query.relevant:
            continue
        ranking = [passages_of.get((query.id, chunk), frozenset()) for chunk in ranked[query.id]]
        values = query_metrics(ranking, [p.grade for p in query.relevant], CUTOFF)
        rows.append((query.language, values.ndcg, values.recall, values.reciprocal_rank))
    by_language = {
        language: mean([ndcg for lang, ndcg, _, _ in rows if lang == language])
        for language in sorted({lang for lang, *_ in rows})
    }
    return (
        mean([r[1] for r in rows]),
        mean([r[2] for r in rows]),
        mean([r[3] for r in rows]),
        by_language,
    )


def sweep(
    dataset: Dataset,
    candidates: Mapping[str, Mapping[str, Sequence[uuid.UUID]]],
    passages_of: Mapping[tuple[str, uuid.UUID], frozenset[int]],
    ks: Iterable[int] = RRF_KS,
    depths: Iterable[int] = DEPTHS,
    weights: Iterable[float] = KEYWORD_WEIGHTS,
) -> list[SweepRow]:
    """`candidates[query_id][method]` is that method's ranking (best first)."""
    rows = []
    for method in ("semantic", "keyword"):
        ranked = {qid: lists[method][:CUTOFF] for qid, lists in candidates.items()}
        ndcg, recall, mrr, by_language = _score(dataset, ranked, passages_of)
        rows.append(SweepRow(f"{method} only", None, None, None, ndcg, recall, mrr, by_language))
    for k in ks:
        for depth in depths:
            for weight in weights:
                ranked = {
                    qid: weighted_rrf(
                        {method: ids[:depth] for method, ids in lists.items()},
                        k,
                        {"semantic": 1.0, "keyword": weight},
                        CUTOFF,
                    )
                    for qid, lists in candidates.items()
                }
                ndcg, recall, mrr, by_language = _score(dataset, ranked, passages_of)
                rows.append(SweepRow("rrf", k, depth, weight, ndcg, recall, mrr, by_language))
    return rows
