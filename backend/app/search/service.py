"""Search orchestration: query embedding, candidate retrieval, fusion and result assembly.

The API process owns one :class:`SearchService`. It holds the query-embedding model, which
is loaded once (lazily, or at startup with ``SEARCH_PRELOAD_MODEL=true``) and then shared by
all requests. Model inference and text analysis are CPU-bound, so they run in worker
threads; inference is serialised on a single dedicated thread, which also bounds the memory
used by concurrent searches. Database work for one search runs in a single REPEATABLE READ
transaction, so every result comes from one consistent snapshot even while documents are
being reindexed or deleted.
"""

import asyncio
import contextlib
import logging
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.embeddings.base import EmbeddingModelInfo, EmbeddingProvider
from app.ingestion.errors import ErrorCode, IngestionError
from app.search.analyzer import QueryAnalysis, analyze_query
from app.search.retrieval import (
    Candidate,
    ChunkRecord,
    FusedCandidate,
    SearchFilters,
    incompatible_document_count,
    keyword_candidates,
    load_chunks,
    reciprocal_rank_fusion,
    semantic_candidates,
)
from app.search.snippets import Snippet, make_snippet

logger = logging.getLogger(__name__)

# After a failed model load, report the model as unavailable for this long instead of
# retrying the (possibly slow) load on every request.
_MODEL_RETRY_SECONDS = 30.0


class SearchMode(StrEnum):
    SEMANTIC = "semantic"
    KEYWORD = "keyword"
    HYBRID = "hybrid"


class ScoreType(StrEnum):
    COSINE_SIMILARITY = "cosine_similarity"
    BM25 = "bm25"
    RRF = "rrf"


class SearchUnavailableError(Exception):
    """Semantic search was requested but the embedding model cannot be used."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class QueryTooLongError(Exception):
    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


@dataclass(frozen=True)
class SearchRequest:
    query: str
    mode: SearchMode
    top_k: int
    filters: SearchFilters
    min_similarity: float | None = None


@dataclass(frozen=True)
class SearchHit:
    rank: int
    score: float
    chunk: ChunkRecord
    snippet: Snippet
    semantic_rank: int | None
    semantic_similarity: float | None
    keyword_rank: int | None
    keyword_score: float | None

    @property
    def methods(self) -> list[str]:
        found = []
        if self.semantic_rank is not None:
            found.append(SearchMode.SEMANTIC.value)
        if self.keyword_rank is not None:
            found.append(SearchMode.KEYWORD.value)
        return found


@dataclass
class SearchOutcome:
    mode: SearchMode
    score_type: ScoreType
    hits: list[SearchHit]
    query_terms: tuple[str, ...]
    warnings: list[str] = field(default_factory=list)
    semantic_available: bool = True
    timings_ms: dict[str, float] = field(default_factory=dict)


class QueryEmbedder:
    """Loads the embedding model once and embeds queries on one dedicated thread.

    The load runs as a background task. A search waits for it at most `load_wait_seconds`
    and then reports the model as unavailable instead of hanging (the first load can mean a
    multi-gigabyte download); the load carries on and later searches use the model.
    """

    def __init__(self, provider: EmbeddingProvider, load_wait_seconds: float = 20.0) -> None:
        self.provider = provider
        self.load_wait_seconds = load_wait_seconds
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="query-embedder")
        self._info: EmbeddingModelInfo | None = None
        self._loading: asyncio.Future[EmbeddingModelInfo] | None = None
        self._failed_at: float | None = None
        self._failure: str | None = None

    def _check_cooldown(self) -> None:
        if (
            self._failed_at is not None
            and time.monotonic() - self._failed_at < _MODEL_RETRY_SECONDS
        ):
            raise SearchUnavailableError(self._failure or "The embedding model is unavailable.")

    async def load(self) -> EmbeddingModelInfo:
        if self._info is not None:
            return self._info
        self._check_cooldown()
        if self._loading is None:
            self._loading = asyncio.ensure_future(self._load())
            # Retrieve a failure even when every waiter has timed out, so it is not logged
            # as "never retrieved"; _load has already recorded and logged it.
            self._loading.add_done_callback(lambda task: task.cancelled() or task.exception())
        try:
            return await asyncio.wait_for(asyncio.shield(self._loading), self.load_wait_seconds)
        except TimeoutError:
            raise SearchUnavailableError(
                "The embedding model is still loading; try again shortly."
            ) from None

    async def _load(self) -> EmbeddingModelInfo:
        loop = asyncio.get_running_loop()
        try:
            info = await loop.run_in_executor(self._executor, self.provider.load)
        except IngestionError as exc:
            self._failed_at, self._failure = time.monotonic(), exc.message
            logger.warning("query_model_unavailable", extra={"error_code": exc.code.value})
            raise SearchUnavailableError(exc.message) from exc
        except Exception as exc:
            # Any other failure is treated the same way, so hybrid search still falls back to
            # keyword results and the cooldown applies.
            message = f"The embedding model could not be loaded ({type(exc).__name__})."
            self._failed_at, self._failure = time.monotonic(), message
            logger.exception("query_model_load_failed")
            raise SearchUnavailableError(message) from exc
        finally:
            self._loading = None
        self._failed_at = self._failure = None
        self._info = info
        return info

    async def embed(self, query: str) -> tuple[EmbeddingModelInfo, list[float]]:
        info = await self.load()
        loop = asyncio.get_running_loop()
        try:
            vectors = await loop.run_in_executor(self._executor, self.provider.embed, [query])
        except IngestionError as exc:
            if exc.code is ErrorCode.INPUT_TOO_LONG:
                raise QueryTooLongError(
                    f"The query is longer than the embedding model accepts "
                    f"({self.provider.max_input_tokens} tokens)."
                ) from exc
            logger.warning("query_embedding_failed", extra={"error_code": exc.code.value})
            raise SearchUnavailableError(exc.message) from exc
        except Exception as exc:
            logger.exception("query_embedding_failed")
            raise SearchUnavailableError(f"Query embedding failed ({type(exc).__name__}).") from exc
        return info, vectors[0]

    def close(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


class SearchService:
    def __init__(self, settings: Settings, provider: EmbeddingProvider) -> None:
        self.settings = settings
        self.embedder = QueryEmbedder(provider, settings.search_model_load_wait_seconds)

    async def warm_up(self) -> None:
        """Load the model and the analyzer ahead of the first search (best effort)."""
        from app.search.analyzer import warm_up as warm_up_analyzer

        await asyncio.to_thread(warm_up_analyzer)
        # A failure is already logged; semantic search reports it per request.
        with contextlib.suppress(SearchUnavailableError):
            await self.embedder.load()

    async def search(self, session: AsyncSession, request: SearchRequest) -> SearchOutcome:
        started = time.perf_counter()
        timings: dict[str, float] = {}
        settings = self.settings
        analysis: QueryAnalysis = await asyncio.to_thread(analyze_query, request.query)
        timings["analyze_ms"] = _ms(started)

        warnings: list[str] = []
        semantic_wanted = request.mode in (SearchMode.SEMANTIC, SearchMode.HYBRID)
        keyword_wanted = request.mode in (SearchMode.KEYWORD, SearchMode.HYBRID)
        model: EmbeddingModelInfo | None = None
        query_vector: list[float] | None = None
        semantic_available = True
        if semantic_wanted:
            step = time.perf_counter()
            try:
                model, query_vector = await self.embedder.embed(request.query)
            except SearchUnavailableError as exc:
                if request.mode is SearchMode.SEMANTIC:
                    raise
                semantic_available = False
                warnings.append(
                    f"Semantic search is unavailable ({exc.message}); "
                    "these results come from keyword search only."
                )
            timings["embed_ms"] = _ms(step)
        if keyword_wanted and analysis.is_empty:
            warnings.append("The query has no searchable words for keyword search.")

        depth = (
            request.top_k if request.mode is not SearchMode.HYBRID else settings.search_fusion_depth
        )
        depth = max(depth, request.top_k)
        semantic: list[Candidate] = []
        keyword: list[Candidate] = []
        # One snapshot for all queries of this search; nothing is written, so it is rolled back.
        await session.connection(execution_options={"isolation_level": "REPEATABLE READ"})
        try:
            if model is not None and query_vector is not None:
                step = time.perf_counter()
                semantic = await semantic_candidates(
                    session,
                    query_vector,
                    model,
                    request.filters,
                    depth,
                    request.min_similarity,
                )
                excluded = await incompatible_document_count(session, model)
                if excluded:
                    warnings.append(
                        f"{excluded} indexed document(s) were embedded with a different model "
                        "or revision and are not included in semantic search; reindex them."
                    )
                timings["semantic_ms"] = _ms(step)
            if keyword_wanted and not analysis.is_empty:
                step = time.perf_counter()
                keyword = await keyword_candidates(
                    session,
                    analysis,
                    request.filters,
                    depth,
                    settings.search_bm25_k1,
                    settings.search_bm25_b,
                )
                timings["keyword_ms"] = _ms(step)
            ranked = self._rank(request, semantic, keyword)
            records = await load_chunks(session, [entry.chunk_id for entry in ranked])
        finally:
            await session.rollback()

        step = time.perf_counter()
        hits = await asyncio.to_thread(self._hits, ranked, records, analysis.terms)
        timings["snippets_ms"] = _ms(step)
        timings["total_ms"] = _ms(started)
        score_type = {
            SearchMode.SEMANTIC: ScoreType.COSINE_SIMILARITY,
            SearchMode.KEYWORD: ScoreType.BM25,
            SearchMode.HYBRID: ScoreType.RRF,
        }[request.mode]
        logger.info(
            "search_completed",
            extra={
                "mode": request.mode.value,
                "results": len(hits),
                "semantic_candidates": len(semantic),
                "keyword_candidates": len(keyword),
                "duration_ms": timings["total_ms"],
            },
        )
        return SearchOutcome(
            mode=request.mode,
            score_type=score_type,
            hits=hits,
            query_terms=analysis.terms,
            warnings=warnings,
            semantic_available=semantic_available,
            timings_ms=timings,
        )

    def _rank(
        self, request: SearchRequest, semantic: list[Candidate], keyword: list[Candidate]
    ) -> list[FusedCandidate]:
        if request.mode is SearchMode.HYBRID:
            rankings = {SearchMode.SEMANTIC.value: semantic, SearchMode.KEYWORD.value: keyword}
            return reciprocal_rank_fusion(rankings, self.settings.search_rrf_k, request.top_k)
        method = request.mode.value
        source = semantic if request.mode is SearchMode.SEMANTIC else keyword
        return [
            FusedCandidate(c.chunk_id, c.score, {method: c.rank}, {method: c.score})
            for c in source[: request.top_k]
        ]

    def _hits(
        self,
        ranked: list[FusedCandidate],
        records: dict[uuid.UUID, ChunkRecord],
        terms: tuple[str, ...],
    ) -> list[SearchHit]:
        hits = []
        for entry in ranked:
            record = records.get(entry.chunk_id)
            if record is None:  # cannot happen inside one snapshot; skip defensively
                continue
            semantic = SearchMode.SEMANTIC.value
            keyword = SearchMode.KEYWORD.value
            hits.append(
                SearchHit(
                    rank=len(hits) + 1,
                    score=entry.score,
                    chunk=record,
                    snippet=make_snippet(record.text, terms, self.settings.search_snippet_chars),
                    semantic_rank=entry.ranks.get(semantic),
                    semantic_similarity=entry.scores.get(semantic),
                    keyword_rank=entry.ranks.get(keyword),
                    keyword_score=entry.scores.get(keyword),
                )
            )
        return hits

    def close(self) -> None:
        self.embedder.close()


def _ms(since: float) -> float:
    return round((time.perf_counter() - since) * 1000, 2)
