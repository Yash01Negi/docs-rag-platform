# Keyword search latency vs corpus size

Measured with `python -m evaluation.keyword_benchmark --sizes 10000,50000,100000` in the development
sandbox (4 CPUs, PostgreSQL 17.8 + pgvector 0.8.1 in Docker, default settings), after the
BM25 statistics CTEs were made MATERIALIZED. Each size times the 51 evaluation queries 3
times in keyword mode through SearchService. The corpus is the 60 evaluation chunks copied
until the table holds the target size, so every term is unusually frequent: a pessimistic
case for per-query document-frequency counting.

| Chunks | Samples | p50 ms | p95 ms | Max ms |
|---:|---:|---:|---:|---:|
| 10,020 | 153 | 23.2 | 47.5 | 57.9 |
| 50,040 | 153 | 69.7 | 145.5 | 206.8 |
| 100,020 | 153 | 128.4 | 317.5 | 468.0 |

Before the MATERIALIZED fix, one query took 24.9 s at 50,040 chunks (EXPLAIN ANALYZE): the
planner inlined the term-statistics CTE and recomputed each term's document frequency for
every matching row.
