"""Candidate retrieval: exact vector search, BM25 keyword search and Reciprocal Rank Fusion.

All SQL here uses bound parameters only; user input never becomes SQL text. Every ranking
ends with the chunk ID as a tie-breaker, so equal scores always come back in the same order.

Which chunks are searchable: chunks only exist for a document's committed index (the worker
replaces a document's chunks in one transaction), so a half-written index is never visible.
A document that is being reindexed keeps serving its previous complete index until the new
one commits, and a deleted document's chunks are gone with it.
"""

import math
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.embeddings.base import EmbeddingModelInfo
from app.search.analyzer import QueryAnalysis, any_term_query, phrase_query, term_query


@dataclass(frozen=True)
class SearchFilters:
    document_ids: tuple[uuid.UUID, ...] | None = None
    language: str | None = None


@dataclass(frozen=True)
class Candidate:
    chunk_id: uuid.UUID
    rank: int  # 1-based position in this method's ranking
    score: float


@dataclass(frozen=True)
class FusedCandidate:
    chunk_id: uuid.UUID
    score: float  # the RRF score: a rank-based sum, not a probability or a similarity
    ranks: dict[str, int] = field(default_factory=dict)
    scores: dict[str, float] = field(default_factory=dict)


def _filter_sql(filters: SearchFilters, params: dict[str, object]) -> str:
    clauses = []
    if filters.document_ids is not None:
        clauses.append("c.document_id = ANY(CAST(:document_ids AS uuid[]))")
        params["document_ids"] = list(filters.document_ids)
    if filters.language is not None:
        clauses.append("c.language = :language")
        params["language"] = filters.language
    return "".join(f" AND {clause}" for clause in clauses)


def _vector_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(repr(float(value)) for value in vector) + "]"


async def semantic_candidates(
    session: AsyncSession,
    query_vector: Sequence[float],
    model: EmbeddingModelInfo,
    filters: SearchFilters,
    limit: int,
    min_similarity: float | None,
) -> list[Candidate]:
    """Chunks closest to the query vector by cosine similarity (exact search).

    Only chunks embedded by the same model, revision and dimension as the query are
    compared; vectors from another model live in a different space.
    """
    params: dict[str, object] = {
        "query": _vector_literal(query_vector),
        "model": model.name,
        "revision": model.revision,
        "dimension": model.dimension,
        "limit": limit,
    }
    threshold = ""
    if min_similarity is not None:
        # cosine distance = 1 - cosine similarity
        threshold = " AND (c.embedding <=> CAST(:query AS vector)) <= :max_distance"
        params["max_distance"] = 1.0 - min_similarity
    sql = (
        "SELECT c.id, 1 - (c.embedding <=> CAST(:query AS vector)) AS similarity "
        "FROM chunks c JOIN documents d ON d.id = c.document_id "
        "WHERE d.embedding_model = :model AND d.embedding_model_revision = :revision "
        "AND d.embedding_dimension = :dimension"
        + _filter_sql(filters, params)
        + threshold
        + " ORDER BY c.embedding <=> CAST(:query AS vector), c.id LIMIT :limit"
    )
    rows = (await session.execute(text(sql), params)).all()
    candidates = []
    for rank, row in enumerate(rows, start=1):
        similarity = float(row.similarity)
        if not math.isfinite(similarity):
            raise ValueError("the database returned a non-finite similarity")
        candidates.append(Candidate(row.id, rank, similarity))
    return candidates


async def incompatible_document_count(session: AsyncSession, model: EmbeddingModelInfo) -> int:
    """Indexed documents whose vectors come from another model or revision than the query's."""
    return int(
        await session.scalar(
            text(
                "SELECT count(*) FROM documents WHERE chunk_count > 0 AND NOT ("
                "embedding_model IS NOT DISTINCT FROM :model AND "
                "embedding_model_revision IS NOT DISTINCT FROM :revision AND "
                "embedding_dimension IS NOT DISTINCT FROM :dimension)"
            ),
            {"model": model.name, "revision": model.revision, "dimension": model.dimension},
        )
        or 0
    )


async def keyword_candidates(
    session: AsyncSession,
    analysis: QueryAnalysis,
    filters: SearchFilters,
    limit: int,
    k1: float,
    b: float,
) -> list[Candidate]:
    """Chunks ranked by Okapi BM25 over the analyzer's terms.

    A chunk matches if it contains any query term (and every quoted phrase). Term
    frequencies come from the stored ``tsvector`` positions, document frequencies and the
    average length from all indexed chunks, so filters narrow the results without changing
    the scores. IDF uses the non-negative form ``ln(1 + (N - df + 0.5) / (df + 0.5))``.
    """
    if analysis.is_empty:
        return []
    params: dict[str, object] = {
        "terms": list(analysis.terms),
        "term_queries": [term_query(term) for term in analysis.terms],
        "any_query": any_term_query(analysis.terms),
        "limit": limit,
        "k1": k1,
        "b": b,
    }
    phrase_filter = ""
    if analysis.phrases:
        params["phrase_query"] = " & ".join(f"({phrase_query(p)})" for p in analysis.phrases)
        phrase_filter = " AND c.search_vector @@ CAST(:phrase_query AS tsquery)"
    # Only fixed SQL fragments are concatenated; every value is a bound parameter.
    sql = (
        "WITH stats AS ("  # noqa: S608
        "  SELECT count(*)::float8 AS n, coalesce(avg(search_length), 0)::float8 AS avgdl"
        "  FROM chunks"
        "), terms AS ("
        "  SELECT t.term, (SELECT count(*) FROM chunks x"
        "                  WHERE x.search_vector @@ CAST(t.q AS tsquery))::float8 AS df"
        "  FROM unnest(CAST(:terms AS text[]), CAST(:term_queries AS text[])) AS t(term, q)"
        "), matches AS ("
        "  SELECT c.id, c.search_vector, c.search_length FROM chunks c"
        "  WHERE c.search_vector @@ CAST(:any_query AS tsquery)"
        + phrase_filter
        + _filter_sql(filters, params)
        + ") SELECT m.id, sum("
        "    ln(1 + (s.n - t.df + 0.5) / (t.df + 0.5))"
        "    * (cardinality(u.positions) * (:k1 + 1))"
        "    / (cardinality(u.positions)"
        "       + :k1 * (1 - :b + :b * m.search_length / greatest(s.avgdl, 1)))"
        "  ) AS score"
        " FROM matches m CROSS JOIN stats s"
        " CROSS JOIN LATERAL unnest(m.search_vector) AS u(lexeme, positions, weights)"
        " JOIN terms t ON t.term = u.lexeme"
        " GROUP BY m.id ORDER BY score DESC, m.id LIMIT :limit"
    )
    rows = (await session.execute(text(sql), params)).all()
    candidates = []
    for rank, row in enumerate(rows, start=1):
        score = float(row.score)
        if not math.isfinite(score):
            raise ValueError("the database returned a non-finite BM25 score")
        candidates.append(Candidate(row.id, rank, score))
    return candidates


def reciprocal_rank_fusion(
    rankings: dict[str, Sequence[Candidate]], k: int, limit: int
) -> list[FusedCandidate]:
    """Combine rankings with Reciprocal Rank Fusion: ``score = Σ 1 / (k + rank)``.

    Only ranks matter, so BM25 scores and cosine similarities never have to be put on a
    common scale. A chunk found by several methods appears once. Ties are broken by the
    best single rank, then by chunk ID.
    """
    fused: dict[uuid.UUID, FusedCandidate] = {}
    for method, candidates in rankings.items():
        for candidate in candidates:
            entry = fused.get(candidate.chunk_id)
            if entry is None:
                entry = FusedCandidate(candidate.chunk_id, 0.0)
                fused[candidate.chunk_id] = entry
            if method in entry.ranks:
                raise ValueError(f"chunk {candidate.chunk_id} appears twice in {method}")
            entry.ranks[method] = candidate.rank
            entry.scores[method] = candidate.score
    results = [
        FusedCandidate(
            entry.chunk_id,
            math.fsum(1.0 / (k + rank) for rank in entry.ranks.values()),
            entry.ranks,
            entry.scores,
        )
        for entry in fused.values()
    ]
    results.sort(key=lambda r: (-r.score, min(r.ranks.values()), str(r.chunk_id)))
    return results[:limit]


@dataclass(frozen=True)
class ChunkRecord:
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    file_type: str
    document_language: str | None
    page_count: int | None
    ordinal: int
    text: str
    language: str
    page_start: int | None
    page_end: int | None
    section: str | None


async def load_chunks(
    session: AsyncSession, chunk_ids: Sequence[uuid.UUID]
) -> dict[uuid.UUID, ChunkRecord]:
    if not chunk_ids:
        return {}
    rows = (
        await session.execute(
            text(
                "SELECT c.id, c.document_id, c.ordinal, c.text, c.language, c.page_start, "
                "c.page_end, c.section, d.filename, d.file_type, d.language AS document_language, "
                "d.page_count FROM chunks c JOIN documents d ON d.id = c.document_id "
                "WHERE c.id = ANY(CAST(:ids AS uuid[]))"
            ),
            {"ids": list(chunk_ids)},
        )
    ).all()
    return {
        row.id: ChunkRecord(
            chunk_id=row.id,
            document_id=row.document_id,
            filename=row.filename,
            file_type=row.file_type,
            document_language=row.document_language,
            page_count=row.page_count,
            ordinal=row.ordinal,
            text=row.text,
            language=row.language,
            page_start=row.page_start,
            page_end=row.page_end,
            section=row.section,
        )
        for row in rows
    }


async def unknown_document_ids(
    session: AsyncSession, document_ids: Sequence[uuid.UUID]
) -> list[uuid.UUID]:
    found = set(
        (
            await session.execute(
                text("SELECT id FROM documents WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": list(document_ids)},
            )
        ).scalars()
    )
    return [document_id for document_id in document_ids if document_id not in found]
