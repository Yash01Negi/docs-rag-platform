# ADR 0006: Exact vector search, Reciprocal Rank Fusion, no reranker, semantic by default

- **Status:** Accepted
- **Date:** 2026-10-12

## Context

Phase 3 adds search over the chunks indexed in Phase 2. Four choices had to be made, and
each was measured rather than assumed:

1. Whether semantic search uses an approximate nearest-neighbour (ANN) index or exact
   search.
2. How semantic and keyword results are combined.
3. Whether a reranking model runs after retrieval.
4. Which mode a search uses when the caller does not choose.

Measurements come from two tools in `backend/evaluation/`, and the numbers are in
[docs/evaluation/](../evaluation/README.md):

- `python -m evaluation.run`: 13 English, Japanese and mixed documents and 51 judged
  queries, run with the real BAAI/bge-m3 model on a GitHub Actions runner (4 vCPU, CPU only).
- `python -m evaluation.ann_benchmark`: exact search against pgvector HNSW on seeded
  synthetic 1024-dimension unit vectors, run on a GitHub Actions runner (up to 100,000 rows)
  and in the development sandbox (up to 200,000 rows).

## Decision 1: exact cosine search, no vector index yet

Semantic search orders chunks by pgvector's exact cosine distance (`<=>`) with ties broken
by chunk ID. No HNSW or IVFFlat index is created.

Measured on GitHub Actions (4 vCPU, PostgreSQL 17.8, pgvector 0.8.1, default settings;
50 queries per size; LIMIT 50). The table is the latest run; recall differs between runs
because the parallel HNSW build produces a different graph each time, and the other run is
in brackets:

| Rows | Exact p50 | Exact p95 | HNSW build | HNSW index | HNSW p50 (ef_search 100) | HNSW recall@10 / @50 |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| 10,000 | 53 ms | 56 ms | 4.6 s | 78 MB | 2.0 ms | 1.000 / 1.000 (1.000 / 1.000) |
| 50,000 | 333 ms | 345 ms | 18.6 s | 391 MB | 2.2 ms | 0.992 / 0.998 (0.998 / 0.999) |
| 100,000 | 668 ms | 682 ms | 39.9 s | 781 MB | 2.7 ms | 0.956 / 0.954 (0.998 / 0.992) |

The sandbox run agrees and adds 200,000 rows, where HNSW recall drops to 0.894 at
ef_search 100 and needs ef_search 200 to reach 0.980 (exact p50 804 ms).

Why exact for now:

- **It is fast enough at this project's scale.** A chunk is about 400 tokens, so 10,000
  chunks is several thousand pages, and exact search takes about 50 ms there. Query
  embedding on CPU costs more: a semantic search took about 185 ms (p50) in the
  evaluation, where the vector scan over 60 chunks is negligible.
- **It has perfect recall** and no tuning parameter. HNSW's recall depends on ef_search
  on the data and on the build, and falls as the table grows (0.956 at 100,000 rows in one
  build and 0.998 in another; 0.894 at 200,000 rows, all with ef_search 100 on the
  synthetic data).
- **It works with filters.** Semantic search always filters by embedding model and
  revision, and optionally by document and language. An HNSW scan returns the nearest
  `ef_search` rows *before* those filters, so a selective filter can return fewer than
  `top_k` results unless pgvector 0.8's iterative scans are enabled and tuned.
- **It keeps ties deterministic.** The query orders by `distance, chunk_id`. An HNSW index
  can only serve `ORDER BY distance`, so ties would have to be resolved in Python.
- **ef_search must be at least the fusion depth.** With the default ef_search of 40, an
  HNSW search with LIMIT 50 returned only 40 rows in every run.

**When to add HNSW.** When the compatible chunk count passes roughly 17,000 (where exact
search reaches about 100 ms on 4 vCPUs, interpolating the measurements above), or when semantic latency matters more than
perfect recall. The change is: a migration creating
`USING hnsw (embedding vector_cosine_ops)`, ordering only by distance, `SET LOCAL
hnsw.ef_search` to at least 100 and at least the fusion depth, `hnsw.iterative_scan =
relaxed_order` for filtered searches, and re-running the evaluation to confirm recall.

## Decision 2: Reciprocal Rank Fusion for hybrid search

Hybrid search fuses up to `SEARCH_FUSION_DEPTH` (50) candidates per method with Reciprocal
Rank Fusion, `Σ 1 / (k + rank)` with k = `SEARCH_RRF_K` (60), deduplicated by chunk ID,
ties broken by best single rank and then chunk ID.

RRF only uses ranks, so BM25 scores (unbounded, query-dependent) and cosine similarities
(narrow range, model-dependent) never have to be normalized onto one scale, which is the
fragile part of score-based fusion. k = 60 is the value from the original paper (Cormack,
Clarke and Büttcher, 2009).

**What the evaluation showed.** On the 47 judged queries with relevant passages, with the
real model:

| Mode | nDCG@10 | Recall@10 | MRR@10 |
| --- | ---: | ---: | ---: |
| Semantic | 0.927 | 0.989 | 0.934 |
| Hybrid (RRF, k 60, depth 50) | 0.874 | 0.968 | 0.878 |
| Keyword | 0.802 | 0.830 | 0.841 |

Hybrid search was *worse* than semantic search. With equal weights, RRF ranks a chunk that
both methods placed in the middle above a chunk only one method placed first. Keyword
search contributes noise for cross-language and paraphrased queries (an English question
about a Japanese passage shares no words with it), and that noise outranks semantic search's
correct first result. Example: "how many days a week can I work remotely" is first in
semantic search and outside the hybrid top 10.

The evaluation re-fused the stored candidate lists for 63 combinations of k (1 to 100),
depth (10 to 50) and keyword weight (1, 0.5, 0.25). Equal-weight RRF stayed between 0.874
and 0.902 nDCG@10 for every k and depth; the best weighted setting (k 1, keyword weight
0.25) reached 0.924, still not above semantic search alone. The defaults were therefore not
tuned: picking the best of 63 settings on 47 queries would fit this dataset, not the
problem.

RRF stays the hybrid method, with the standard defaults, because it is the robust choice
when the two scores cannot be compared, and because hybrid search is the mode that keeps
working (as keyword search) when the embedding model is unavailable.

## Decision 3: no reranker

No cross-encoder or LLM reranker runs after retrieval. A reranker costs a second model in
memory and roughly one model call per candidate per query on CPU. The evidence that would
justify one is not there: semantic search already places a relevant chunk first for 42 of
the 47 judged queries and within the top 10 for all of them (MRR@10 0.934, Recall@10
0.989). The
evaluation set is the place to show a gain before adding one: a reranker should beat
semantic nDCG@10 by a clear margin on it, without losing the Japanese or mixed slices, at an
acceptable CPU latency.

## Decision 4: semantic search by default

Requests without a `mode`, and the Search page, use `SEARCH_DEFAULT_MODE`, which defaults to
`semantic`: it scored best on every language slice (English 0.904, Japanese 0.930, mixed
0.963 nDCG@10). Yash chose this on 2026-10-12 after seeing the numbers above. Hybrid and
keyword stay available per request, and the setting makes the default a one-line change.

## Consequences

- Semantic latency grows linearly with the number of compatible chunks. The benchmark
  script is in the repository, so the decision can be revisited with real data.
- The evaluation set is small (51 queries) and written for this project. It has few
  identifier-style queries (error codes, part numbers), where keyword matching usually
  helps most. Hybrid search should be re-evaluated when such queries are added.
- Semantic search returns results even for questions the documents cannot answer (all 4
  unanswerable queries got 10 results in semantic and hybrid mode; keyword returned 1.2 on
  average). Callers that need "no answer" behaviour can set `min_similarity`; a calibrated
  threshold is future work for answer generation.
- Hybrid rankings of tied chunks depend on chunk IDs, which are random per ingestion, so
  hybrid metrics vary slightly between evaluation runs (nDCG@10 0.874, 0.866 and
  0.874 in three runs on the same commit range). Semantic and keyword results were identical across runs.
