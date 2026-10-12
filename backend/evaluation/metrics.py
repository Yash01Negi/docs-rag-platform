"""Ranking metrics over judged passages.

A query is judged as a list of passages, each with a grade (2 = answers the query, 1 =
related). A retrieved chunk can contain several judged passages (or none), so a ranking is
given as one set of passage indexes per retrieved chunk, best first.

Each passage is credited once: if two chunks contain the same passage (chunk overlap), the
second adds nothing to recall or nDCG. Precision counts every chunk that contains any judged
passage, since such a chunk is relevant to read.
"""

import math
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class QueryMetrics:
    recall: float
    precision: float
    reciprocal_rank: float
    ndcg: float


def _gain(grade: int) -> float:
    return float(2**grade - 1)


def _discount(position: int) -> float:
    """Discount for a 1-based rank position."""
    return 1.0 / math.log2(position + 1)


def query_metrics(ranking: Sequence[frozenset[int]], grades: Sequence[int], k: int) -> QueryMetrics:
    """Recall@k, Precision@k, reciprocal rank@k and nDCG@k for one judged query.

    `ranking[i]` holds the indexes (into `grades`) of the judged passages contained in the
    chunk at rank i + 1. `grades` must not be empty: queries with no relevant passage have
    no defined recall or nDCG and are reported separately.
    """
    if k < 1:
        raise ValueError("k must be at least 1")
    if not grades:
        raise ValueError("query_metrics needs at least one judged passage")
    if any(grade < 1 for grade in grades):
        raise ValueError("judged passages must have a grade of at least 1")
    top = ranking[:k]
    if any(index < 0 or index >= len(grades) for passages in top for index in passages):
        raise ValueError("ranking refers to a passage that is not judged")

    credited: set[int] = set()
    relevant_chunks = 0
    first_relevant: int | None = None
    dcg = 0.0
    for position, passages in enumerate(top, start=1):
        if not passages:
            continue
        relevant_chunks += 1
        if first_relevant is None:
            first_relevant = position
        new = passages - credited
        if new:
            dcg += _gain(max(grades[index] for index in new)) * _discount(position)
            credited |= new
    ideal = sorted(grades, reverse=True)[:k]
    idcg = sum(_gain(grade) * _discount(position) for position, grade in enumerate(ideal, 1))
    return QueryMetrics(
        recall=len(credited) / len(grades),
        precision=relevant_chunks / k,
        reciprocal_rank=0.0 if first_relevant is None else 1.0 / first_relevant,
        ndcg=dcg / idcg,
    )


def percentile(values: Sequence[float], p: float) -> float:
    """Nearest-rank percentile (p in 0-100): always one of the observed values."""
    if not values:
        raise ValueError("percentile of an empty sequence")
    if not 0 <= p <= 100:
        raise ValueError("p must be between 0 and 100")
    ordered = sorted(values)
    rank = max(1, math.ceil(p / 100 * len(ordered)))
    return ordered[rank - 1]


def mean(values: Sequence[float]) -> float:
    if not values:
        raise ValueError("mean of an empty sequence")
    return sum(values) / len(values)
