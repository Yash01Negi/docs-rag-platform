# Exact vs HNSW search benchmark (GitHub Actions)

Output of `python -m evaluation.ann_benchmark --sizes 10000,50000,100000 --queries 50`,
copied from two runs of the temporary retrieval workflow on this branch. Both used the same
seeded synthetic data. The HNSW index build uses parallel workers, so the graph (and
therefore its recall) differs between builds; the exact search results are identical.

- PostgreSQL 17.8 (Debian 17.8-1.pgdg12+1), pgvector 0.8.1; maintenance_work_mem 1GB, shared_buffers 128MB, max_parallel_workers_per_gather 2, max_parallel_maintenance_workers 2
- Host: Linux-6.17.0-1022-azure-x86_64-with-glibc2.39, 4 CPUs (GitHub-hosted ubuntu-24.04 runner)
- Synthetic unit vectors, 1024 dimensions, 200 clusters, noise sd 0.7; 50 queries per size; LIMIT 50
- HNSW m=16, ef_construction=64; recall is measured against the exact result

## Run 3 (job 114797308870, commit 3767562)

| Rows | Table MB | Exact p50 ms | Exact p95 ms | HNSW build s | Index MB |
|---:|---:|---:|---:|---:|---:|
| 10,000 | 53.7 | 52.96 | 55.86 | 4.6 | 78.1 |
| 50,000 | 267.9 | 333.06 | 345.23 | 18.6 | 390.6 |
| 100,000 | 535.6 | 667.66 | 682.43 | 39.9 | 781.3 |

| Rows | ef_search | p50 ms | p95 ms | Recall@10 | Recall@50 | Min rows returned |
|---:|---:|---:|---:|---:|---:|---:|
| 10,000 | 40 | 0.91 | 1.12 | 1.000 | 0.800 | 40 |
| 10,000 | 64 | 1.35 | 1.56 | 1.000 | 1.000 | 50 |
| 10,000 | 100 | 2.01 | 2.25 | 1.000 | 1.000 | 50 |
| 10,000 | 200 | 3.27 | 3.58 | 1.000 | 1.000 | 50 |
| 50,000 | 40 | 2.36 | 3.44 | 0.950 | 0.768 | 40 |
| 50,000 | 64 | 1.98 | 2.88 | 0.972 | 0.977 | 50 |
| 50,000 | 100 | 2.19 | 3.33 | 0.992 | 0.998 | 50 |
| 50,000 | 200 | 2.63 | 3.9 | 0.992 | 0.998 | 50 |
| 100,000 | 40 | 2.76 | 3.44 | 0.900 | 0.737 | 40 |
| 100,000 | 64 | 2.49 | 3.15 | 0.934 | 0.928 | 50 |
| 100,000 | 100 | 2.68 | 4.04 | 0.956 | 0.954 | 50 |
| 100,000 | 200 | 3.14 | 4.97 | 0.976 | 0.975 | 50 |

## Run 1 (job 114789086174, commit 80318d5)

| Rows | Table MB | Exact p50 ms | Exact p95 ms | HNSW build s | Index MB |
|---:|---:|---:|---:|---:|---:|
| 10,000 | 53.7 | 49.31 | 51.14 | 4.0 | 78.1 |
| 50,000 | 267.9 | 312.27 | 322.31 | 17.6 | 390.6 |
| 100,000 | 535.6 | 623.82 | 648.89 | 39.5 | 781.3 |

| Rows | ef_search | p50 ms | p95 ms | Recall@10 | Recall@50 | Min rows returned |
|---:|---:|---:|---:|---:|---:|---:|
| 10,000 | 40 | 0.81 | 0.92 | 1.000 | 0.800 | 40 |
| 10,000 | 64 | 1.02 | 1.09 | 1.000 | 1.000 | 50 |
| 10,000 | 100 | 1.33 | 1.49 | 1.000 | 1.000 | 50 |
| 10,000 | 200 | 2.18 | 2.39 | 1.000 | 1.000 | 50 |
| 50,000 | 40 | 2.05 | 2.64 | 0.996 | 0.800 | 40 |
| 50,000 | 64 | 1.61 | 2.07 | 0.998 | 0.998 | 50 |
| 50,000 | 100 | 1.67 | 2.05 | 0.998 | 0.999 | 50 |
| 50,000 | 200 | 1.93 | 2.25 | 0.998 | 0.999 | 50 |
| 100,000 | 40 | 2.94 | 3.98 | 0.922 | 0.752 | 40 |
| 100,000 | 64 | 2.24 | 3.13 | 0.974 | 0.967 | 50 |
| 100,000 | 100 | 2.41 | 3.38 | 0.998 | 0.992 | 50 |
| 100,000 | 200 | 2.79 | 4.28 | 0.998 | 0.994 | 50 |
