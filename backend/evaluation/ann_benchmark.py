"""Benchmark exact cosine search against a pgvector HNSW index on synthetic vectors.

    uv run python -m evaluation.ann_benchmark --database-url postgresql://... \\
        --sizes 10000,50000,100000

For each corpus size the script creates a table shaped like `chunks.embedding`
(vector(1024)), fills it with clustered unit vectors generated inside PostgreSQL (seeded,
so runs are repeatable), and measures:

- exact search: ORDER BY embedding <=> query LIMIT n with no vector index (what YashRAG
  runs today);
- HNSW (m=16, ef_construction=64): build time, index size, query latency and recall
  against the exact result at several ef_search values.

The vectors are synthetic: clusters of noisy unit vectors, not BGE-M3 embeddings, so the
recall numbers indicate how HNSW behaves at this dimension, not on real text. The table is
dropped at the end. Results go to stdout as Markdown and, with --output, as JSON.
"""

import argparse
import json
import os
import platform
import statistics
import sys
import time
from collections.abc import Sequence
from pathlib import Path

import psycopg

from evaluation.metrics import percentile

TABLE = "ann_benchmark_vectors"
QUERY_TABLE = "ann_benchmark_queries"
DIMENSION = 1024


def _generate(
    conn: psycopg.Connection, size: int, queries: int, clusters: int, noise: float
) -> None:
    conn.execute(f"DROP TABLE IF EXISTS {TABLE}, {QUERY_TABLE}")
    conn.execute(f"CREATE TABLE {TABLE} (id bigint PRIMARY KEY, embedding vector({DIMENSION}))")
    conn.execute(f"CREATE TABLE {QUERY_TABLE} (id int PRIMARY KEY, embedding vector({DIMENSION}))")
    conn.execute("SELECT setseed(0.42)")
    conn.execute(
        "CREATE TEMP TABLE ann_centroids AS SELECT i, "
        "(SELECT array_agg(random_normal()) FROM generate_series(1, %s::int) "
        "WHERE i IS NOT NULL)::vector AS v FROM generate_series(0, %s::int) i",
        (DIMENSION, clusters - 1),
    )
    noisy = (
        "l2_normalize(c.v + (SELECT array_agg(random_normal(0, %(noise)s::float8)) "
        "FROM generate_series(1, %(dim)s::int) WHERE n IS NOT NULL)::vector)"
    )
    batch = 5000
    for start in range(0, size, batch):
        conn.execute(
            f"INSERT INTO {TABLE} SELECT n, {noisy} FROM generate_series(%(a)s::int, %(b)s::int) n "  # noqa: S608
            "JOIN ann_centroids c ON c.i = n %% %(clusters)s",
            {
                "a": start,
                "b": min(start + batch, size) - 1,
                "clusters": clusters,
                "noise": noise,
                "dim": DIMENSION,
            },
        )
    # Queries come from the same clusters but are not in the table.
    conn.execute(
        f"INSERT INTO {QUERY_TABLE} SELECT n, {noisy} FROM generate_series(0, %(q)s::int) n "  # noqa: S608
        "JOIN ann_centroids c ON c.i = (n * 7919) %% %(clusters)s",
        {"q": queries - 1, "clusters": clusters, "noise": noise, "dim": DIMENSION},
    )
    conn.execute("DROP TABLE ann_centroids")
    conn.execute(f"VACUUM ANALYZE {TABLE}")


def _search(conn: psycopg.Connection, query_id: int, limit: int) -> tuple[list[int], float]:
    started = time.perf_counter()
    rows = conn.execute(
        f"SELECT id FROM {TABLE} ORDER BY embedding <=> "  # noqa: S608
        f"(SELECT embedding FROM {QUERY_TABLE} WHERE id = %s) LIMIT %s",
        (query_id, limit),
    ).fetchall()
    return [row[0] for row in rows], (time.perf_counter() - started) * 1000


def _uses_index(conn: psycopg.Connection) -> bool:
    plan = conn.execute(
        f"EXPLAIN SELECT id FROM {TABLE} ORDER BY embedding <=> "  # noqa: S608
        f"(SELECT embedding FROM {QUERY_TABLE} WHERE id = 0) LIMIT 50"
    ).fetchall()
    return any("ann_benchmark_hnsw" in row[0] for row in plan)


def _latency(values: Sequence[float]) -> dict[str, float]:
    return {
        "p50": round(percentile(values, 50), 2),
        "p95": round(percentile(values, 95), 2),
        "mean": round(statistics.fmean(values), 2),
    }


def benchmark_size(
    conn: psycopg.Connection,
    size: int,
    queries: int,
    limit: int,
    ef_values: Sequence[int],
    clusters: int,
    noise: float,
) -> dict:  # type: ignore[type-arg]
    started = time.perf_counter()
    _generate(conn, size, queries, clusters, noise)
    generated_s = time.perf_counter() - started
    table_bytes = conn.execute("SELECT pg_total_relation_size(%s)", (TABLE,)).fetchone()[0]  # type: ignore[index]

    # Exact: no vector index exists yet. One untimed pass warms the cache.
    assert not _uses_index(conn)  # noqa: S101
    for query_id in range(min(3, queries)):
        _search(conn, query_id, limit)
    truth: list[list[int]] = []
    exact_ms: list[float] = []
    for query_id in range(queries):
        ids, ms = _search(conn, query_id, limit)
        truth.append(ids)
        exact_ms.append(ms)

    started = time.perf_counter()
    conn.execute(
        f"CREATE INDEX ann_benchmark_hnsw ON {TABLE} "
        "USING hnsw (embedding vector_cosine_ops) WITH (m = 16, ef_construction = 64)"
    )
    build_s = time.perf_counter() - started
    index_bytes = conn.execute("SELECT pg_relation_size('ann_benchmark_hnsw')").fetchone()[0]  # type: ignore[index]
    # Small tables can make the planner prefer a sequential scan; measure the index anyway.
    conn.execute("SET enable_seqscan = off")
    assert _uses_index(conn)  # noqa: S101

    hnsw = []
    for ef in ef_values:
        conn.execute(f"SET hnsw.ef_search = {int(ef)}")
        for query_id in range(min(3, queries)):
            _search(conn, query_id, limit)
        latencies: list[float] = []
        recall_10: list[float] = []
        recall_all: list[float] = []
        returned: list[int] = []
        for query_id in range(queries):
            ids, ms = _search(conn, query_id, limit)
            latencies.append(ms)
            returned.append(len(ids))
            recall_10.append(len(set(ids[:10]) & set(truth[query_id][:10])) / 10)
            recall_all.append(len(set(ids) & set(truth[query_id])) / limit)
        hnsw.append(
            {
                "ef_search": ef,
                "latency_ms": _latency(latencies),
                "recall@10": round(statistics.fmean(recall_10), 4),
                f"recall@{limit}": round(statistics.fmean(recall_all), 4),
                "min_results_returned": min(returned),
            }
        )
    conn.execute("RESET hnsw.ef_search")
    conn.execute("RESET enable_seqscan")
    conn.execute(f"DROP TABLE {TABLE}, {QUERY_TABLE}")
    return {
        "size": size,
        "generate_s": round(generated_s, 1),
        "table_mb": round(table_bytes / 2**20, 1),
        "exact_latency_ms": _latency(exact_ms),
        "hnsw_build_s": round(build_s, 1),
        "hnsw_index_mb": round(index_bytes / 2**20, 1),
        "hnsw": hnsw,
    }


def render(result: dict) -> str:  # type: ignore[type-arg]
    meta = result["metadata"]
    limit = meta["limit"]
    lines = [
        "# Exact vs HNSW search benchmark",
        "",
        f"- PostgreSQL {meta['postgres']}, pgvector {meta['pgvector']}; "
        f"maintenance_work_mem {meta['maintenance_work_mem']}, "
        f"shared_buffers {meta['shared_buffers']}, "
        f"max_parallel_workers_per_gather {meta['parallel_workers']}, "
        f"max_parallel_maintenance_workers {meta['parallel_maintenance_workers']}",
        f"- Host: {meta['host']}",
        f"- Synthetic unit vectors, {meta['dimension']} dimensions, {meta['clusters']} clusters, "
        f"noise sd {meta['noise']}; {meta['queries']} queries per size; LIMIT {limit}",
        "- HNSW m=16, ef_construction=64; recall is measured against the exact result",
        "",
        "| Rows | Table MB | Exact p50 ms | Exact p95 ms | HNSW build s | Index MB |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for row in result["sizes"]:
        lines.append(
            f"| {row['size']:,} | {row['table_mb']} | {row['exact_latency_ms']['p50']} | "
            f"{row['exact_latency_ms']['p95']} | {row['hnsw_build_s']} | {row['hnsw_index_mb']} |"
        )
    lines += [
        "",
        f"| Rows | ef_search | p50 ms | p95 ms | Recall@10 | Recall@{limit} | Min rows returned |",
        "|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in result["sizes"]:
        for entry in row["hnsw"]:
            lines.append(
                f"| {row['size']:,} | {entry['ef_search']} | {entry['latency_ms']['p50']} | "
                f"{entry['latency_ms']['p95']} | {entry['recall@10']:.3f} | "
                f"{entry[f'recall@{limit}']:.3f} | {entry['min_results_returned']} |"
            )
    if result.get("failure"):
        lines += ["", f"**Stopped early** at {result['failure']}"]
    return "\n".join(lines) + "\n"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--database-url", required=True, help="libpq URL (postgresql://...)")
    parser.add_argument("--sizes", default="10000,50000,100000")
    parser.add_argument("--queries", type=int, default=50)
    parser.add_argument("--limit", type=int, default=50, help="rows per search (fusion depth)")
    parser.add_argument("--ef-search", default="40,64,100,200")
    parser.add_argument("--clusters", type=int, default=200)
    parser.add_argument("--noise", type=float, default=0.7)
    parser.add_argument("--maintenance-work-mem", default="1GB")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    sizes = [int(size) for size in args.sizes.split(",")]
    ef_values = [int(ef) for ef in args.ef_search.split(",")]

    url = args.database_url.replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "SELECT set_config('maintenance_work_mem', %s, false)", (args.maintenance_work_mem,)
        )

        def setting(name: str) -> str:
            return str(conn.execute(f"SHOW {name}").fetchone()[0])  # type: ignore[index]

        metadata = {
            "postgres": setting("server_version"),
            "pgvector": conn.execute(
                "SELECT extversion FROM pg_extension WHERE extname = 'vector'"
            ).fetchone()[0],  # type: ignore[index]
            "maintenance_work_mem": setting("maintenance_work_mem"),
            "shared_buffers": setting("shared_buffers"),
            "parallel_workers": setting("max_parallel_workers_per_gather"),
            "parallel_maintenance_workers": setting("max_parallel_maintenance_workers"),
            "host": f"{platform.platform()}, {os.cpu_count()} CPUs",
            "dimension": DIMENSION,
            "clusters": args.clusters,
            "noise": args.noise,
            "queries": args.queries,
            "limit": args.limit,
        }
        results = []
        failure: str | None = None
        try:
            for size in sizes:
                print(f"benchmarking {size:,} rows...", file=sys.stderr, flush=True)
                results.append(
                    benchmark_size(
                        conn, size, args.queries, args.limit, ef_values, args.clusters, args.noise
                    )
                )
        except psycopg.Error as exc:
            # Keep the sizes that finished; report the one that did not.
            failure = f"{size:,} rows: {type(exc).__name__}: {exc}".strip()
        finally:
            conn.execute(f"DROP TABLE IF EXISTS {TABLE}, {QUERY_TABLE}")
    result = {"metadata": metadata, "sizes": results, "failure": failure}
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(render(result))
    if failure:
        print(f"error: stopped at {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
