"""Search endpoints (Phase 3): semantic, keyword and hybrid retrieval over indexed chunks.

Results are ranked chunks from uploaded documents. Nothing here generates text: snippets are
verbatim excerpts of the chunk, and highlights mark words that match the query.
"""

import logging
import unicodedata
import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.exc import SQLAlchemyError

from app.api.v1.documents import Session
from app.api.v1.schemas import ErrorResponse
from app.core.errors import ApiError
from app.core.logging import request_id_var
from app.search.retrieval import SearchFilters, unknown_document_ids
from app.search.service import (
    QueryTooLongError,
    ScoreType,
    SearchMode,
    SearchRequest,
    SearchService,
    SearchUnavailableError,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/search", tags=["search"])

SCORE_DESCRIPTIONS = {
    ScoreType.COSINE_SIMILARITY: (
        "Cosine similarity between the query and chunk embeddings, from -1 to 1 (higher is "
        "closer). Comparable between results of the same query."
    ),
    ScoreType.BM25: (
        "Okapi BM25 keyword relevance, 0 or more. Only meaningful for ranking the results of "
        "one query; not comparable across queries and not a probability."
    ),
    ScoreType.RRF: (
        "Reciprocal Rank Fusion score: the sum of 1 / (k + rank) over the methods that found "
        "the chunk. It reflects ranks only; it is not a similarity or a probability."
    ),
}


class SearchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    query: str = Field(min_length=1, description="Search text in Japanese, English or both.")
    mode: SearchMode = Field(
        default=SearchMode.HYBRID,
        description="`semantic` (meaning), `keyword` (words and phrases) or `hybrid` (both).",
    )
    top_k: int | None = Field(
        default=None, ge=1, description="Number of results. Defaults to SEARCH_DEFAULT_TOP_K."
    )
    document_ids: list[uuid.UUID] | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="Only search these documents. Unknown IDs are rejected.",
    )
    language: Literal["ja", "en", "mixed"] | None = Field(
        default=None, description="Only return chunks detected as this language."
    )
    min_similarity: float | None = Field(
        default=None,
        ge=-1,
        le=1,
        description=(
            "Minimum cosine similarity for semantic candidates (semantic and hybrid modes)."
        ),
    )

    @field_validator("query")
    @classmethod
    def _clean_query(cls, value: str) -> str:
        if "\x00" in value:
            raise ValueError("query must not contain NUL characters")
        # NFC keeps the text as typed; control characters other than whitespace are removed.
        value = unicodedata.normalize("NFC", value)
        value = "".join(
            char for char in value if char in "\t\n\r" or unicodedata.category(char) != "Cc"
        ).strip()
        if not value:
            raise ValueError("query must contain at least one non-space character")
        return value

    @field_validator("document_ids")
    @classmethod
    def _unique_ids(cls, value: list[uuid.UUID] | None) -> list[uuid.UUID] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("document_ids must not contain duplicates")
        return value


class MethodRank(BaseModel):
    rank: int = Field(description="1-based rank in this method's own candidate list.")
    score: float = Field(description="Cosine similarity (semantic) or BM25 score (keyword).")


class SearchResultItem(BaseModel):
    rank: int
    score: float
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    file_type: str
    document_language: str | None
    page_count: int | None
    chunk_ordinal: int = Field(description="Position of the chunk within its document.")
    language: str = Field(description="Detected language of the chunk text.")
    page_start: int | None
    page_end: int | None
    section: str | None
    text: str = Field(description="The full chunk text, verbatim.")
    snippet: str = Field(description="A verbatim excerpt of the chunk around the matches.")
    snippet_start: int = Field(description="Offset of the snippet within `text`.")
    highlights: list[tuple[int, int]] = Field(
        description="[start, end) character ranges in `snippet` that match query words."
    )
    retrieval_methods: list[Literal["semantic", "keyword"]] = Field(
        description="Which methods returned this chunk."
    )
    semantic: MethodRank | None
    keyword: MethodRank | None


class SearchResponse(BaseModel):
    query: str
    mode: SearchMode
    score_type: ScoreType
    score_description: str
    top_k: int
    results: list[SearchResultItem]
    query_terms: list[str] = Field(description="Terms the keyword analyzer found in the query.")
    semantic_available: bool = Field(
        description="False when hybrid search fell back to keyword-only results."
    )
    warnings: list[str]
    request_id: str | None
    timings_ms: dict[str, float] = Field(description="Server-side timings, for diagnostics.")


class SearchOptions(BaseModel):
    modes: list[SearchMode]
    default_mode: SearchMode
    default_top_k: int
    max_top_k: int
    max_query_chars: int
    rrf_k: int
    fusion_depth: int


def _service(request: Request) -> SearchService:
    return request.app.state.search


Service = Annotated[SearchService, Depends(_service)]


@router.get("/options", response_model=SearchOptions)
async def search_options(request: Request) -> SearchOptions:
    """Limits and defaults, so clients can validate before searching."""
    settings = request.app.state.settings
    return SearchOptions(
        modes=list(SearchMode),
        default_mode=SearchMode.HYBRID,
        default_top_k=settings.search_default_top_k,
        max_top_k=settings.search_max_top_k,
        max_query_chars=settings.search_max_query_chars,
        rrf_k=settings.search_rrf_k,
        fusion_depth=settings.search_fusion_depth,
    )


@router.post(
    "",
    response_model=SearchResponse,
    responses={
        422: {"model": ErrorResponse, "description": "Invalid query, filters or unknown IDs"},
        503: {"model": ErrorResponse, "description": "Embedding model or database unavailable"},
    },
)
async def search(
    body: SearchBody, request: Request, session: Session, service: Service
) -> SearchResponse:
    """Search indexed chunks.

    * `semantic`: BGE-M3 query embedding, exact cosine-similarity search in pgvector.
    * `keyword`: Japanese/English analyzer terms ranked with BM25; put words in `"..."` or
      `「...」` to require them as a phrase.
    * `hybrid` (default): both, combined with Reciprocal Rank Fusion. If the embedding model
      is unavailable, hybrid returns keyword results with `semantic_available: false`.
    """
    settings = request.app.state.settings
    top_k = body.top_k or settings.search_default_top_k
    if top_k > settings.search_max_top_k:
        raise ApiError(422, "invalid_top_k", f"top_k must be at most {settings.search_max_top_k}.")
    if len(body.query) > settings.search_max_query_chars:
        raise ApiError(
            422,
            "query_too_long",
            f"The query must be at most {settings.search_max_query_chars} characters.",
        )
    if body.min_similarity is not None and body.mode is SearchMode.KEYWORD:
        raise ApiError(
            422,
            "invalid_filter",
            "min_similarity applies to semantic and hybrid search only.",
        )
    try:
        if body.document_ids:
            missing = await unknown_document_ids(session, body.document_ids)
            await session.rollback()
            if missing:
                raise ApiError(
                    422,
                    "unknown_document",
                    f"Unknown document ID(s): {', '.join(str(i) for i in missing[:5])}.",
                )
        outcome = await service.search(
            session,
            SearchRequest(
                query=body.query,
                mode=body.mode,
                top_k=top_k,
                filters=SearchFilters(
                    document_ids=tuple(body.document_ids) if body.document_ids else None,
                    language=body.language,
                ),
                min_similarity=body.min_similarity,
            ),
        )
    except SearchUnavailableError as exc:
        raise ApiError(
            503,
            "semantic_search_unavailable",
            f"Semantic search is unavailable: {exc.message} Try keyword search.",
        ) from exc
    except QueryTooLongError as exc:
        raise ApiError(422, "query_too_long", exc.message) from exc
    except SQLAlchemyError as exc:
        logger.warning("search_database_error", extra={"error_type": type(exc).__name__})
        raise ApiError(503, "database_unavailable", "The search database is unavailable.") from exc

    return SearchResponse(
        query=body.query,
        mode=outcome.mode,
        score_type=outcome.score_type,
        score_description=SCORE_DESCRIPTIONS[outcome.score_type],
        top_k=top_k,
        results=[
            SearchResultItem(
                rank=hit.rank,
                score=hit.score,
                chunk_id=hit.chunk.chunk_id,
                document_id=hit.chunk.document_id,
                filename=hit.chunk.filename,
                file_type=hit.chunk.file_type,
                document_language=hit.chunk.document_language,
                page_count=hit.chunk.page_count,
                chunk_ordinal=hit.chunk.ordinal,
                language=hit.chunk.language,
                page_start=hit.chunk.page_start,
                page_end=hit.chunk.page_end,
                section=hit.chunk.section,
                text=hit.chunk.text,
                snippet=hit.snippet.text,
                snippet_start=hit.snippet.start,
                highlights=hit.snippet.highlights,
                retrieval_methods=hit.methods,  # type: ignore[arg-type]
                semantic=MethodRank(rank=hit.semantic_rank, score=hit.semantic_similarity)
                if hit.semantic_rank is not None and hit.semantic_similarity is not None
                else None,
                keyword=MethodRank(rank=hit.keyword_rank, score=hit.keyword_score)
                if hit.keyword_rank is not None and hit.keyword_score is not None
                else None,
            )
            for hit in outcome.hits
        ],
        query_terms=list(outcome.query_terms),
        semantic_available=outcome.semantic_available,
        warnings=outcome.warnings,
        request_id=request_id_var.get(),
        timings_ms=outcome.timings_ms,
    )
