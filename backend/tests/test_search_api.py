"""Search API against a real PostgreSQL + pgvector database, with the fake embedder.

Fake vectors carry no meaning (they are seeded by a hash of the text), so these tests check
the mechanics of semantic search (exact-match recall, ordering, filters, failures), not its
quality. Retrieval quality with the real model is measured by ``evaluation/``.
"""

import asyncio
import threading
import time
import uuid
from collections.abc import Sequence

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.db.session import create_engine as create_async_engine
from app.db.session import create_session_factory
from app.embeddings.base import EmbeddingModelInfo
from app.embeddings.fake import FakeEmbeddingProvider
from app.ingestion.errors import ErrorCode, IngestionError
from app.main import create_app
from app.search.analyzer import analyze_query
from app.search.retrieval import SearchFilters, keyword_candidates
from app.search.service import QueryEmbedder, SearchMode, SearchRequest, SearchService
from tests.search_fixtures import SeedChunk, seed_document
from tests.test_documents_api import run_worker_until_idle

pytestmark = pytest.mark.integration

EN_VECTOR = "PostgreSQL stores every chunk with a dense embedding vector for semantic search."
EN_BACKUP = "Nightly backups copy the database to object storage and keep thirty days."
JA_VECTOR = "各チャンクの埋め込みベクトルはPostgreSQLに保存され、意味検索に使われます。"
JA_TOKYO = "東京都庁の会議室で、検索エンジンの導入について議論しました。"
MIXED = "BGE-M3モデルはEnglish and Japaneseの文を同じベクトル空間に埋め込みます。"


def _search(client: TestClient, query: str, **fields: object):  # type: ignore[no-untyped-def]
    return client.post("/api/v1/search", json={"query": query, **fields})


def _ids(response) -> list[str]:  # type: ignore[no-untyped-def]
    assert response.status_code == 200, response.text
    return [item["chunk_id"] for item in response.json()["results"]]


@pytest.fixture
def corpus(db_settings: Settings) -> dict[str, uuid.UUID]:
    docs = {
        "en": seed_document(
            db_settings,
            "vectors.md",
            [SeedChunk(EN_VECTOR, page=2, section="Storage"), EN_BACKUP],
        ),
        "ja": seed_document(db_settings, "検索.txt", [JA_VECTOR, JA_TOKYO]),
        "mixed": seed_document(db_settings, "model.md", [MIXED]),
    }
    return {
        "en_vector": docs["en"].chunk_ids[0],
        "en_backup": docs["en"].chunk_ids[1],
        "ja_vector": docs["ja"].chunk_ids[0],
        "ja_tokyo": docs["ja"].chunk_ids[1],
        "mixed": docs["mixed"].chunk_ids[0],
        "doc_en": docs["en"].id,
        "doc_ja": docs["ja"].id,
        "doc_mixed": docs["mixed"].id,
    }


# --- empty database and basic shape --------------------------------------------------------


@pytest.mark.parametrize("mode", ["semantic", "keyword", "hybrid"])
def test_empty_database_returns_no_results(db_client: TestClient, mode: str) -> None:
    response = _search(db_client, "anything at all", mode=mode)
    assert response.status_code == 200
    body = response.json()
    assert body["results"] == []
    assert body["mode"] == mode
    assert body["request_id"] == response.headers["X-Request-ID"]


def test_result_carries_source_metadata_and_score_semantics(
    db_client: TestClient, corpus: dict[str, uuid.UUID]
) -> None:
    body = _search(db_client, "backups object storage", mode="keyword").json()
    top = body["results"][0]
    assert top["chunk_id"] == str(corpus["en_backup"])
    assert top["document_id"] == str(corpus["doc_en"])
    assert top["filename"] == "vectors.md"
    assert top["chunk_ordinal"] == 1
    assert top["text"] == EN_BACKUP
    assert top["retrieval_methods"] == ["keyword"]
    assert top["keyword"]["rank"] == 1 and top["keyword"]["score"] > 0
    assert top["semantic"] is None
    assert body["score_type"] == "bm25"
    assert "not comparable across queries" in body["score_description"]
    assert set(body["timings_ms"]) >= {"total_ms", "keyword_ms"}


def test_page_and_section_are_returned(db_client: TestClient, corpus: dict[str, uuid.UUID]) -> None:
    top = _search(db_client, "dense embedding", mode="keyword").json()["results"][0]
    assert top["chunk_id"] == str(corpus["en_vector"])
    assert (top["page_start"], top["page_end"], top["section"]) == (2, 2, "Storage")


# --- keyword search ------------------------------------------------------------------------


def test_english_keyword_search_uses_stemming(
    db_client: TestClient, corpus: dict[str, uuid.UUID]
) -> None:
    # "backup" and "copies" only appear as "backups" and "copy".
    assert _ids(_search(db_client, "backup copies", mode="keyword"))[0] == str(corpus["en_backup"])


def test_japanese_keyword_search_without_spaces(
    db_client: TestClient, corpus: dict[str, uuid.UUID]
) -> None:
    ids = _ids(_search(db_client, "検索エンジンの導入", mode="keyword"))
    assert ids[0] == str(corpus["ja_tokyo"])


def test_part_of_a_japanese_compound_matches(
    db_client: TestClient, corpus: dict[str, uuid.UUID]
) -> None:
    # 東京都庁 is one dictionary word; its part 都庁 must still find it.
    assert _ids(_search(db_client, "都庁", mode="keyword")) == [str(corpus["ja_tokyo"])]


def test_mixed_language_query(db_client: TestClient, corpus: dict[str, uuid.UUID]) -> None:
    ids = _ids(_search(db_client, "BGE-M3 ベクトル空間", mode="keyword"))
    assert ids[0] == str(corpus["mixed"])


def test_full_width_query_matches_ascii_text(
    db_client: TestClient, corpus: dict[str, uuid.UUID]
) -> None:
    ids = _ids(_search(db_client, "ＰｏｓｔｇｒｅＳＱＬ", mode="keyword"))
    assert set(ids) == {str(corpus["en_vector"]), str(corpus["ja_vector"])}


def test_rarer_terms_rank_higher_with_bm25(
    db_client: TestClient, corpus: dict[str, uuid.UUID]
) -> None:
    # "postgresql" is in two chunks, "semantic" in one: the chunk with both ranks first.
    ids = _ids(_search(db_client, "postgresql semantic", mode="keyword"))
    assert ids[0] == str(corpus["en_vector"])


def test_bm25_computes_each_term_statistic_once(db_settings: Settings) -> None:
    """Guard the query plan: per-term document frequencies must not be recomputed per row.

    When PostgreSQL inlined the statistics CTE, the df subquery ran once per (chunk, term)
    pair, and a query over 50,000 chunks took 25 seconds.
    """
    seed_document(
        db_settings,
        "many.txt",
        [
            f"Backup policy number {n} covers the database and its restore drills."
            for n in range(40)
        ],
    )
    analysis = analyze_query("database backup restore")
    plans: list[dict] = []  # type: ignore[type-arg]

    async def scenario() -> None:
        engine = create_async_engine(db_settings)
        try:
            async with create_session_factory(engine)() as session:
                execute = session.execute

                async def explain(statement, params=None, *args, **kwargs):  # type: ignore[no-untyped-def]
                    sql = "EXPLAIN (ANALYZE, FORMAT JSON) " + str(statement)
                    result = await execute(text(sql), params)
                    plans.append(result.scalar_one()[0]["Plan"])
                    return await execute(statement, params, *args, **kwargs)

                session.execute = explain  # type: ignore[method-assign]
                found = await keyword_candidates(session, analysis, SearchFilters(), 10, 1.2, 0.75)
                assert len(found) == 10
        finally:
            await engine.dispose()

    asyncio.run(scenario())

    def subplan_loops(node: dict) -> list[int]:  # type: ignore[type-arg]
        loops = [node["Actual Loops"]] if node.get("Subplan Name") else []
        for child in node.get("Plans", []):
            loops += subplan_loops(child)
        return loops

    loops = subplan_loops(plans[0])
    assert loops, "expected the per-term df subquery in the plan"
    assert max(loops) <= len(analysis.terms)


def test_quoted_phrase_requires_adjacent_words(
    db_client: TestClient, db_settings: Settings
) -> None:
    near = seed_document(db_settings, "a.txt", ["The vector search engine is fast."])
    apart = seed_document(db_settings, "b.txt", ["Search the space of every vector quickly."])
    unquoted = set(_ids(_search(db_client, "vector search", mode="keyword")))
    assert unquoted == {str(near.chunk_ids[0]), str(apart.chunk_ids[0])}
    quoted = _ids(_search(db_client, '"vector search"', mode="keyword"))
    assert quoted == [str(near.chunk_ids[0])]


def test_japanese_quoted_phrase(db_client: TestClient, db_settings: Settings) -> None:
    near = seed_document(db_settings, "a.txt", ["埋め込みベクトルを保存します。"])
    seed_document(db_settings, "b.txt", ["ベクトルを埋め込みます。"])
    assert _ids(_search(db_client, "「埋め込みベクトル」", mode="keyword")) == [
        str(near.chunk_ids[0])
    ]


def test_query_without_searchable_words_returns_no_keyword_results(
    db_client: TestClient, corpus: dict[str, uuid.UUID]
) -> None:
    body = _search(db_client, "the of and。、", mode="keyword").json()
    assert body["results"] == []
    assert body["query_terms"] == []
    assert any("no searchable words" in warning for warning in body["warnings"])


def test_highlights_mark_matching_words(
    db_client: TestClient, corpus: dict[str, uuid.UUID]
) -> None:
    top = _search(db_client, "ベクトル", mode="keyword").json()["results"][0]
    marked = [top["snippet"][start:end] for start, end in top["highlights"]]
    assert marked and all(word == "ベクトル" for word in marked)


# --- semantic search -----------------------------------------------------------------------


def test_semantic_search_finds_the_chunk_with_the_same_embedding(
    db_client: TestClient, corpus: dict[str, uuid.UUID]
) -> None:
    body = _search(db_client, JA_TOKYO, mode="semantic").json()
    top = body["results"][0]
    assert top["chunk_id"] == str(corpus["ja_tokyo"])
    assert top["score"] == pytest.approx(1.0, abs=1e-6)
    assert top["semantic"] == {"rank": 1, "score": top["score"]}
    assert body["score_type"] == "cosine_similarity"
    scores = [item["score"] for item in body["results"]]
    assert scores == sorted(scores, reverse=True)


def test_min_similarity_drops_distant_chunks(
    db_client: TestClient, corpus: dict[str, uuid.UUID]
) -> None:
    ids = _ids(_search(db_client, JA_TOKYO, mode="semantic", min_similarity=0.5))
    assert ids == [str(corpus["ja_tokyo"])]


def test_documents_from_another_model_revision_are_excluded_from_semantic_search(
    db_client: TestClient, db_settings: Settings
) -> None:
    old = seed_document(db_settings, "old.txt", [EN_BACKUP], revision="an-older-revision")
    body = _search(db_client, EN_BACKUP, mode="semantic").json()
    assert body["results"] == []
    assert any("different model or revision" in warning for warning in body["warnings"])
    # Keyword search does not depend on the embedding model.
    assert _ids(_search(db_client, "backups", mode="keyword")) == [str(old.chunk_ids[0])]


# --- hybrid search -------------------------------------------------------------------------


def test_hybrid_fuses_both_methods_with_rrf(
    db_client: TestClient, corpus: dict[str, uuid.UUID]
) -> None:
    body = _search(db_client, EN_BACKUP).json()
    assert body["mode"] == "hybrid" and body["score_type"] == "rrf"
    top = body["results"][0]
    assert top["chunk_id"] == str(corpus["en_backup"])
    assert top["retrieval_methods"] == ["semantic", "keyword"]
    assert top["score"] == pytest.approx(2 / 61)  # rank 1 in both: 1/(60+1) twice
    ids = [item["chunk_id"] for item in body["results"]]
    assert len(ids) == len(set(ids))  # each chunk once
    for item in body["results"]:
        expected = sum(1 / (60 + item[m]["rank"]) for m in item["retrieval_methods"])
        assert item["score"] == pytest.approx(expected)


def test_hybrid_falls_back_to_keyword_when_the_model_is_unavailable(
    db_settings: Settings, corpus: dict[str, uuid.UUID]
) -> None:
    with TestClient(create_app(db_settings)) as client:
        client.app.state.search.embedder.provider = BrokenProvider()  # type: ignore[attr-defined]
        response = _search(client, "backups")
        assert response.status_code == 200
        body = response.json()
        assert body["semantic_available"] is False
        assert body["results"][0]["chunk_id"] == str(corpus["en_backup"])
        assert body["results"][0]["retrieval_methods"] == ["keyword"]
        assert any("Semantic search is unavailable" in w for w in body["warnings"])

        semantic = _search(client, "backups", mode="semantic")
        assert semantic.status_code == 503
        assert semantic.json()["error"]["code"] == "semantic_search_unavailable"
        assert "Traceback" not in semantic.text


def test_search_does_not_hang_while_the_model_is_loading(
    make_settings, clean_database: str, corpus: dict[str, uuid.UUID]
) -> None:
    settings = make_settings(database_url=clean_database, search_model_load_wait_seconds=0.2)
    provider = SlowProvider()
    with TestClient(create_app(settings)) as client:
        client.app.state.search.embedder.provider = provider  # type: ignore[attr-defined]
        try:
            hybrid = _search(client, "backups")
            assert hybrid.status_code == 200
            assert hybrid.json()["semantic_available"] is False
            assert any("still loading" in w for w in hybrid.json()["warnings"])
            assert hybrid.json()["results"][0]["chunk_id"] == str(corpus["en_backup"])

            semantic = _search(client, "backups", mode="semantic")
            assert semantic.status_code == 503
            assert "still loading" in semantic.json()["error"]["message"]
        finally:
            provider.release.set()
        # The load carried on in the background; once it finishes semantic search works.
        for _ in range(50):
            response = _search(client, "backups", mode="semantic")
            if response.status_code == 200:
                break
            time.sleep(0.05)
        assert response.status_code == 200
        assert response.json()["results"][0]["retrieval_methods"] == ["semantic"]
        assert provider.loads == 1


def test_query_model_is_loaded_once_for_concurrent_searches() -> None:
    provider = SlowProvider()
    embedder = QueryEmbedder(provider, load_wait_seconds=5)

    async def scenario() -> None:
        searches = [asyncio.create_task(embedder.embed(f"query {n}")) for n in range(5)]
        await asyncio.sleep(0.05)
        provider.release.set()
        results = await asyncio.gather(*searches)
        assert all(len(vector) == 1024 for _, vector in results)

    try:
        asyncio.run(scenario())
    finally:
        embedder.close()
    assert provider.loads == 1


class SlowProvider(FakeEmbeddingProvider):
    """A provider whose load blocks until the test releases it, like a model download."""

    def __init__(self) -> None:
        super().__init__(1024)
        self.release = threading.Event()
        self.loads = 0

    def load(self) -> EmbeddingModelInfo:
        # embed() also calls load(); count only the calls that had to wait for the model.
        if not self.release.is_set():
            self.loads += 1
        if not self.release.wait(timeout=10):
            raise IngestionError(ErrorCode.EMBEDDING_MODEL_UNAVAILABLE, "Test load timed out.")
        return super().load()


class BrokenProvider(FakeEmbeddingProvider):
    def __init__(self) -> None:
        super().__init__(1024)

    def load(self) -> EmbeddingModelInfo:
        raise IngestionError(ErrorCode.EMBEDDING_MODEL_UNAVAILABLE, "The model is not cached.")


# --- filters and validation ----------------------------------------------------------------


def test_document_filter(db_client: TestClient, corpus: dict[str, uuid.UUID]) -> None:
    ids = _ids(
        _search(db_client, "PostgreSQL", mode="keyword", document_ids=[str(corpus["doc_ja"])])
    )
    assert ids == [str(corpus["ja_vector"])]


def test_language_filter(db_client: TestClient, corpus: dict[str, uuid.UUID]) -> None:
    ids = _ids(_search(db_client, "PostgreSQL", mode="keyword", language="en"))
    assert ids == [str(corpus["en_vector"])]


def test_unknown_document_id_is_rejected(db_client: TestClient, corpus: object) -> None:
    unknown = str(uuid.uuid4())
    response = _search(db_client, "PostgreSQL", document_ids=[unknown])
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "unknown_document"
    assert unknown in response.json()["error"]["message"]


@pytest.mark.parametrize(
    ("body", "code"),
    [
        ({"query": ""}, "validation_error"),
        ({"query": "   \n "}, "validation_error"),
        ({"query": "a\x00b"}, "validation_error"),
        ({"query": "x", "mode": "fuzzy"}, "validation_error"),
        ({"query": "x", "top_k": 0}, "validation_error"),
        ({"query": "x", "top_k": 51}, "invalid_top_k"),
        ({"query": "x", "document_ids": ["not-a-uuid"]}, "validation_error"),
        ({"query": "x", "document_ids": []}, "validation_error"),
        ({"query": "x", "language": "fr"}, "validation_error"),
        ({"query": "x", "min_similarity": 2}, "validation_error"),
        ({"query": "x", "mode": "keyword", "min_similarity": 0.3}, "invalid_filter"),
        ({"query": "x", "unexpected": 1}, "validation_error"),
        ({"query": "x" * 1001}, "query_too_long"),
    ],
)
def test_invalid_requests_are_rejected(db_client: TestClient, body: dict, code: str) -> None:
    response = db_client.post("/api/v1/search", json=body)
    assert response.status_code == 422, response.text
    assert response.json()["error"]["code"] == code


def test_duplicate_document_ids_are_rejected(db_client: TestClient) -> None:
    same = str(uuid.uuid4())
    response = _search(db_client, "x", document_ids=[same, same])
    assert response.status_code == 422


def test_top_k_bounds_the_results(db_client: TestClient, db_settings: Settings) -> None:
    seed_document(db_settings, "many.txt", [f"Vector chunk number {n}." for n in range(30)])
    for mode in ("semantic", "keyword", "hybrid"):
        assert len(_ids(_search(db_client, "vector", mode=mode, top_k=7))) == 7


def test_options_endpoint(db_client: TestClient) -> None:
    body = db_client.get("/api/v1/search/options").json()
    assert body["modes"] == ["semantic", "keyword", "hybrid"]
    assert (body["default_top_k"], body["max_top_k"], body["rrf_k"]) == (10, 50, 60)


# --- robustness ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query",
    [
        "'; DROP TABLE chunks; --",
        "vector' OR '1'='1",
        "a & b | !c <-> (d:*",
        "\\' \\\\ '' \"",
        "%_ ILIKE '%'",
    ],
)
def test_hostile_queries_are_treated_as_text(
    db_client: TestClient, db_settings: Settings, corpus: dict[str, uuid.UUID], query: str
) -> None:
    for mode in ("semantic", "keyword", "hybrid"):
        assert _search(db_client, query, mode=mode).status_code == 200
    engine = create_engine(db_settings.database_url.get_secret_value())
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT count(*) FROM chunks")) == 5
    finally:
        engine.dispose()


def test_ranking_is_deterministic_for_ties(db_client: TestClient, db_settings: Settings) -> None:
    # Identical texts in three documents get identical scores in every method.
    seeded = [seed_document(db_settings, f"{n}.txt", [EN_BACKUP]) for n in range(3)]
    expected = sorted(str(doc.chunk_ids[0]) for doc in seeded)
    for mode in ("semantic", "keyword", "hybrid"):
        runs = {tuple(_ids(_search(db_client, EN_BACKUP, mode=mode))) for _ in range(3)}
        assert runs == {tuple(expected)}


def test_deleted_documents_disappear_from_results(
    db_client: TestClient, corpus: dict[str, uuid.UUID]
) -> None:
    assert db_client.delete(f"/api/v1/documents/{corpus['doc_en']}").status_code == 204
    for mode in ("semantic", "keyword", "hybrid"):
        ids = _ids(_search(db_client, EN_BACKUP, mode=mode))
        assert str(corpus["en_backup"]) not in ids
        assert str(corpus["en_vector"]) not in ids


def test_document_being_reindexed_keeps_serving_its_previous_index(
    db_client: TestClient, corpus: dict[str, uuid.UUID]
) -> None:
    assert db_client.post(f"/api/v1/documents/{corpus['doc_en']}/reindex").status_code == 202
    ids = _ids(_search(db_client, "backups", mode="keyword"))
    assert ids == [str(corpus["en_backup"])]


def test_pending_document_without_an_index_is_not_searchable(
    db_client: TestClient, db_settings: Settings
) -> None:
    response = db_client.post(
        "/api/v1/documents",
        files={"file": ("new.txt", b"Unindexed backups text.", "text/plain")},
    )
    assert response.status_code == 202
    assert _ids(_search(db_client, "unindexed", mode="keyword")) == []


def test_database_failure_returns_503(make_settings) -> None:  # type: ignore[no-untyped-def]
    with TestClient(create_app(make_settings())) as client:
        response = _search(client, "anything", mode="keyword")
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "database_unavailable"
    assert "127.0.0.1" not in response.text and "password" not in response.text


def test_concurrent_searches_are_consistent(
    db_settings: Settings, corpus: dict[str, uuid.UUID]
) -> None:
    async def run() -> list[list[uuid.UUID]]:
        engine = create_async_engine(db_settings)
        sessions = create_session_factory(engine)
        service = SearchService(db_settings, FakeEmbeddingProvider(1024))

        async def one(mode: SearchMode) -> list[uuid.UUID]:
            async with sessions() as session:
                outcome = await service.search(
                    session, SearchRequest("PostgreSQL ベクトル", mode, 5, SearchFilters())
                )
                return [hit.chunk.chunk_id for hit in outcome.hits]

        try:
            modes: Sequence[SearchMode] = list(SearchMode) * 10
            return await asyncio.gather(*(one(mode) for mode in modes))
        finally:
            service.close()
            await engine.dispose()

    results = asyncio.run(run())
    assert len(results) == 30
    for index, mode in enumerate(list(SearchMode) * 10):
        assert results[index] == results[list(SearchMode).index(mode)]


def test_end_to_end_upload_index_and_search(db_client: TestClient, db_settings: Settings) -> None:
    text_ = (
        "# 検索ガイド\n\n全文検索エンジンは日本語の形態素解析を使います。\n\n"
        "Hybrid search fuses rankings."
    )
    response = db_client.post(
        "/api/v1/documents", files={"file": ("guide.md", text_.encode(), "text/markdown")}
    )
    document_id = response.json()["document"]["id"]
    assert run_worker_until_idle(db_settings) == 1
    for query in ("形態素解析", "rankings fused", "全文検索"):
        results = _search(db_client, query, mode="hybrid").json()["results"]
        assert results and results[0]["document_id"] == document_id
        assert results[0]["section"] == "検索ガイド"
