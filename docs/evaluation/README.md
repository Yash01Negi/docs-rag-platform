# Retrieval evaluation

How YashRAG's search quality and latency are measured, and the results that the Phase 3
decisions ([ADR 0005](../decisions/0005-bilingual-keyword-search.md),
[ADR 0006](../decisions/0006-hybrid-retrieval.md)) rest on.

## Files in this folder

| File | What it is |
| --- | --- |
| [bge-m3-report.md](bge-m3-report.md) | Full report of the latest run with the real BAAI/bge-m3 model (GitHub Actions) |
| [bge-m3-results.json](bge-m3-results.json) | Raw results of that run: metadata, summaries, fusion sweep, every query's ranking and latency |
| [ann-benchmark-github-actions.md](ann-benchmark-github-actions.md) | Exact vs HNSW vector search, two runs on GitHub Actions (up to 100,000 rows) |
| [ann-benchmark-sandbox.md](ann-benchmark-sandbox.md) / [.json](ann-benchmark-sandbox.json) | The same benchmark in the development sandbox (up to 200,000 rows) |
| [keyword-benchmark-sandbox.md](keyword-benchmark-sandbox.md) | Keyword (BM25) latency as the corpus grows to 100,000 chunks |

## The evaluation set

`backend/evaluation/dataset/` (version 1):

- **13 documents**: 6 English (including 2 distractors that share vocabulary with other
  documents but answer nothing), 5 Japanese, 2 mixed Japanese and English. Markdown and
  plain text.
- **51 queries**: 21 English, 19 Japanese, 11 mixed. 47 have at least one relevant
  passage (61 judged passages in total); 4 have none (en-14, en-15, ja-14, mx-10).
- **Query types** (a query can have several): exact_keyword 23, ja_no_spaces 18,
  paraphrase 15, cross_language 15, mixed 11, multi_relevant 8, distractor 6,
  no_relevant 4. `queries.toml` defines each type.
- **Judgments name passages, not chunks.** Each relevant passage is a document plus an
  anchor, a short exact quote from it, with a grade (2 answers the query, 1 is related but
  incomplete). A retrieved chunk is relevant when it contains an anchor, compared after
  NFKC normalization and whitespace collapsing. This keeps the judgments valid when
  chunking or tokenization changes. The harness refuses to run if an anchor is missing
  from its document or from every indexed chunk.
- **Chunking for the evaluation is smaller than production** (120 target tokens, 20
  overlap, 20 minimum, giving 60 chunks) so that each document becomes several chunks and
  ranking is between passages rather than whole documents.

The set was written for this project. It is small, and it has few identifier-style queries
(error codes, part numbers), where keyword matching usually helps most. Use it as a
regression baseline, not as a general benchmark.

## Metrics

All metrics are computed at k = 10 (Recall and Precision are also reported at 5) over the
47 queries with relevant passages, then averaged:

- **Recall@k**: judged passages found in the top k / judged passages.
- **Precision@k**: chunks in the top k containing any judged passage / k.
- **MRR@k**: 1 / rank of the first relevant chunk (0 if none in the top k).
- **nDCG@k**: gain 2^grade − 1 with a log2 rank discount, normalized by the ideal ordering
  of the judged passages. A passage is credited once: a second chunk repeating the same
  passage (from chunk overlap) adds nothing.
- **Queries with no relevant passage** are reported separately: how many returned an empty
  list (the ideal) and the mean number of results.
- **Latency**: wall time of `SearchService.search` in process (query analysis, query
  embedding, SQL, snippets; no HTTP), each query run 3 times after loading the model,
  p50/p95 by nearest rank.

## How to run

From `backend/`, against a migrated PostgreSQL database whose name ends in `_eval` or
`_test` (`--reset` deletes all of its documents; other names are refused):

```bash
# All three modes with the real model (needs the embeddings extra and the model download)
DATABASE_URL=postgresql+psycopg://…/yashrag_eval \
  uv run --extra embeddings python -m evaluation.run --reset

# Keyword mode only, without the model
DATABASE_URL=postgresql+psycopg://…/yashrag_eval ENVIRONMENT=test EMBEDDING_PROVIDER=fake \
  uv run python -m evaluation.run --reset --modes keyword
```

Options: `--modes keyword,semantic,hybrid`, `--repeats 3`, `--output-dir
evaluation/results/latest` (gitignored; holds `report.md` and `results.json`). Semantic and
hybrid modes refuse to run with fake embeddings unless `--allow-fake-embeddings` is given,
and the report then starts with a warning that its semantic numbers are meaningless.

Benchmarks:

```bash
# Exact vs HNSW on seeded synthetic vectors, in a scratch database with the vector extension
uv run python -m evaluation.ann_benchmark --database-url postgresql://…/scratch \
  --sizes 10000,50000,100000 --queries 50

# Keyword latency vs corpus size (after evaluation.run has loaded the corpus)
DATABASE_URL=…/yashrag_eval uv run python -m evaluation.keyword_benchmark \
  --sizes 10000,50000,100000
```

## Results with the real model

Run on a GitHub Actions runner (4 vCPU AMD EPYC 7763, CPU only, PostgreSQL 17.8,
pgvector 0.8.1), BAAI/bge-m3 at revision 5617a9f, commit 3767562. Full report:
[bge-m3-report.md](bge-m3-report.md).

| Mode | nDCG@10 | Recall@10 | Precision@5 | MRR@10 | First relevant at rank 1 | Any relevant in top 10 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| Semantic | **0.927** | **0.989** | **0.238** | **0.934** | 42 of 47 | 47 of 47 |
| Hybrid (RRF k 60, depth 50) | 0.874 | 0.968 | 0.217 | 0.878 | 39 of 47 | 46 of 47 |
| Keyword (BM25) | 0.802 | 0.830 | 0.187 | 0.841 | 37 of 47 | 43 of 47 |

nDCG@10 by query language and type:

| Slice | Keyword | Semantic | Hybrid |
| --- | ---: | ---: | ---: |
| English (19) | 0.755 | 0.904 | 0.815 |
| Japanese (18) | 0.761 | 0.930 | 0.888 |
| Mixed (10) | 0.963 | 0.963 | 0.963 |
| exact_keyword (23) | 0.951 | 0.938 | 0.944 |
| ja_no_spaces (17) | 0.747 | 0.926 | 0.881 |
| paraphrase (15) | 0.663 | 0.937 | 0.816 |
| cross_language (15) | 0.548 | 0.903 | 0.731 |
| multi_relevant (8) | 0.573 | 0.952 | 0.852 |
| distractor (6) | 0.444 | 0.737 | 0.607 |

What this shows:

- **Semantic search is best overall and on every language slice.** It is the default mode
  (`SEARCH_DEFAULT_MODE=semantic`).
- **Keyword search is competitive only where the query repeats the document's words**
  (exact_keyword 0.951, mixed 0.963). It cannot match across languages, and it finds
  Japanese text without spaces through Sudachi segmentation (ja_no_spaces 0.747).
- **Hybrid search is between the two.** Equal-weight RRF lets keyword noise outrank
  semantic search's correct first result on cross-language and paraphrased queries. Example:
  en-13 "how many days a week can I work remotely" is first in semantic search and outside
  the hybrid top 10.
- **No-relevant queries:** keyword returned an empty list for 1 of 4 (1.2 results on
  average); semantic and hybrid always returned 10, because nearest neighbours always exist.
  `min_similarity` can cut them off; it is not calibrated yet.

### Fusion settings

The harness also fetches each method's top 50 candidates per query and re-fuses them
offline for 63 combinations of RRF k (1, 5, 10, 20, 40, 60, 100), depth (10, 20, 50) and
keyword weight (1, 0.5, 0.25; the application uses 1):

- Equal-weight RRF ranged from 0.874 to 0.902 nDCG@10 across all k and depths.
- The best weighted setting (k 1, keyword weight 0.25) reached 0.924, still below semantic
  search alone (0.927).

The defaults were not tuned to the best row: choosing among 63 settings on 47 queries would
fit this dataset. ADR 0006 has the reasoning.

### Latency

In-process `SearchService.search`, 153 samples per mode (51 queries × 3):

| Mode | p50 | p95 |
| --- | ---: | ---: |
| Keyword | 5.1 ms | 13.4 ms |
| Semantic | 185.4 ms | 200.2 ms |
| Hybrid | 186.7 ms | 198.3 ms |

Semantic and hybrid time is almost all query embedding on CPU; the vector scan over 60
chunks is negligible. A GPU or Apple Silicon (MPS) would be faster but has not been measured.

### Run-to-run variation

Three runs on this branch gave identical keyword and semantic results. Hybrid nDCG@10 was
0.874, 0.866 and 0.874: RRF ties are broken by chunk ID, and chunk IDs are random UUIDs
per ingestion, so a tied pair can swap. Latency varies by a few percent between runners
(semantic p50 180 to 185 ms).

## Benchmarks

### Exact vs HNSW vector search

Seeded synthetic unit vectors (1024 dimensions, 200 clusters), 50 queries per size,
LIMIT 50; recall is against the exact result. Latest GitHub Actions run (HNSW m 16,
ef_construction 64, ef_search 100):

| Rows | Exact p50 | HNSW p50 | HNSW recall@10 | HNSW build | Index size |
| ---: | ---: | ---: | ---: | ---: | ---: |
| 10,000 | 53 ms | 2.0 ms | 1.000 | 4.6 s | 78 MB |
| 50,000 | 333 ms | 2.2 ms | 0.992 | 18.6 s | 391 MB |
| 100,000 | 668 ms | 2.7 ms | 0.956 | 39.9 s | 781 MB |

HNSW recall changes between builds (0.998 at 100,000 rows in the other run), falls with
size (0.894 at 200,000 rows in the sandbox), and the default ef_search of 40 returns only
40 rows for LIMIT 50. Exact search stays the choice for now; ADR 0006 says when to switch.

### Keyword latency vs corpus size

Sandbox, 4 CPUs, evaluation chunks copied up to each size (a pessimistic case, since every
term is very frequent):

| Chunks | p50 | p95 |
| ---: | ---: | ---: |
| 10,020 | 23.2 ms | 47.5 ms |
| 50,040 | 69.7 ms | 145.5 ms |
| 100,020 | 128.4 ms | 317.5 ms |

Before the BM25 statistics were computed once per query (MATERIALIZED CTEs), one query took
24.9 s at 50,040 chunks. `test_bm25_computes_each_term_statistic_once` guards the fix.

## Reading the numbers carefully

- 47 judged queries is a small sample: a difference of 0.02 nDCG@10 is one or two queries
  moving a few ranks. Look at the per-query table in the report before drawing conclusions.
- The documents and judgments were written by the same project that built the search, so
  they may favour its choices. Adding real user queries is the best next step.
- Re-run the evaluation after changing the embedding model, chunking, analyzer, BM25 or
  fusion settings, and compare against `bge-m3-results.json`.
