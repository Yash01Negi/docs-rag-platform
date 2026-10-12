"""Measure keyword (BM25) search latency as the number of chunks grows.

    uv run python -m evaluation.run --reset --modes keyword      # loads the evaluation corpus
    uv run python -m evaluation.keyword_benchmark --sizes 10000,50000,100000

BM25 statistics (chunk count, average length, document frequency of each query term) are
computed per query, so keyword latency grows with the corpus. This script copies the
evaluation corpus's documents and chunks (new IDs, same text and search vectors) until the
table holds each requested number of chunks, then times every evaluation query in keyword
mode through SearchService. Copies repeat the same 60 chunks, so every term is far more
frequent than in real text: a pessimistic case for document-frequency counting.

The copies are deleted at the end. Only databases whose name ends in _eval or _test are
accepted, as for `evaluation.run --reset`.
"""

import argparse
import asyncio
import sys
import time
import unicodedata
from collections.abc import Sequence

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from app.core.config import get_settings
from app.db.session import create_engine, create_session_factory
from app.embeddings import create_embedding_provider
from app.search.retrieval import SearchFilters
from app.search.service import SearchMode, SearchRequest, SearchService
from evaluation.dataset import load_dataset
from evaluation.metrics import percentile

COPY_MARKER = "keyword-benchmark-copy"


async def _columns(engine: AsyncEngine, table: str) -> list[str]:
    async with engine.connect() as connection:
        rows = await connection.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = :table ORDER BY ordinal_position"
            ),
            {"table": table},
        )
        return [row[0] for row in rows]


async def _grow(engine: AsyncEngine, target: int) -> int:
    """Copy the original documents (and their chunks) until there are `target` chunks."""
    documents = await _columns(engine, "documents")
    chunks = await _columns(engine, "chunks")
    replaced_doc = {
        "id": "m.new_id",
        "sha256": "encode(sha256(convert_to(m.new_id::text, 'UTF8')), 'hex')",
        "storage_key": f"'{COPY_MARKER}/' || m.new_id",
    }
    replaced_chunk = {"id": "gen_random_uuid()", "document_id": "m.new_id"}
    doc_select = ", ".join(replaced_doc.get(c, f"d.{c}") for c in documents)
    chunk_select = ", ".join(replaced_chunk.get(c, f"c.{c}") for c in chunks)
    async with engine.begin() as connection:
        current = (await connection.execute(text("SELECT count(*) FROM chunks"))).scalar_one()
        per_copy = (
            await connection.execute(
                text(
                    "SELECT count(*) FROM chunks c JOIN documents d ON d.id = c.document_id "
                    "WHERE d.storage_key NOT LIKE :marker"
                ),
                {"marker": f"{COPY_MARKER}/%"},
            )
        ).scalar_one()
        copies = max(0, -(-(target - current) // per_copy))
        if copies == 0:
            return current
        await connection.execute(
            text(
                "CREATE TEMP TABLE copy_map ON COMMIT DROP AS "
                "SELECT d.id AS old_id, gen_random_uuid() AS new_id "
                "FROM documents d CROSS JOIN generate_series(1, :copies) "
                "WHERE d.storage_key NOT LIKE :marker"
            ),
            {"copies": copies, "marker": f"{COPY_MARKER}/%"},
        )
        await connection.execute(
            text(
                f"INSERT INTO documents ({', '.join(documents)}) "  # noqa: S608
                f"SELECT {doc_select} FROM copy_map m JOIN documents d ON d.id = m.old_id"
            )
        )
        await connection.execute(
            text(
                f"INSERT INTO chunks ({', '.join(chunks)}) "  # noqa: S608
                f"SELECT {chunk_select} FROM copy_map m JOIN chunks c ON c.document_id = m.old_id"
            )
        )
        total = (await connection.execute(text("SELECT count(*) FROM chunks"))).scalar_one()
    async with engine.connect() as connection:
        await connection.execution_options(isolation_level="AUTOCOMMIT")
        await connection.execute(text("VACUUM ANALYZE chunks"))
    return total


async def _cleanup(engine: AsyncEngine) -> None:
    async with engine.begin() as connection:
        await connection.execute(
            text("DELETE FROM documents WHERE storage_key LIKE :marker"),
            {"marker": f"{COPY_MARKER}/%"},
        )


async def benchmark(sizes: Sequence[int], repeats: int) -> list[dict[str, float | int]]:
    settings = get_settings()
    database = make_url(settings.database_url.get_secret_value()).database or ""
    if not database.endswith(("_eval", "_test")):
        raise SystemExit(f"refusing to write to database {database!r} (needs _eval or _test)")
    dataset = load_dataset()
    engine = create_engine(settings)
    sessions = create_session_factory(engine)
    service = SearchService(settings, create_embedding_provider(settings))
    results = []
    try:
        async with engine.connect() as connection:
            if not (await connection.execute(text("SELECT count(*) FROM chunks"))).scalar_one():
                raise SystemExit("no chunks: run `python -m evaluation.run --reset` first")
        for size in sizes:
            total = await _grow(engine, size)
            print(f"timing keyword search over {total:,} chunks...", file=sys.stderr, flush=True)
            latencies: list[float] = []
            for _ in range(repeats + 1):
                for query in dataset.queries:
                    request = SearchRequest(
                        query=unicodedata.normalize("NFC", query.text),
                        mode=SearchMode.KEYWORD,
                        top_k=10,
                        filters=SearchFilters(),
                    )
                    async with sessions() as session:
                        started = time.perf_counter()
                        await service.search(session, request)
                        latencies.append((time.perf_counter() - started) * 1000)
            latencies = latencies[len(dataset.queries) :]  # the first pass warms the cache
            results.append(
                {
                    "chunks": total,
                    "samples": len(latencies),
                    "p50_ms": round(percentile(latencies, 50), 1),
                    "p95_ms": round(percentile(latencies, 95), 1),
                    "max_ms": round(max(latencies), 1),
                }
            )
    finally:
        await _cleanup(engine)
        service.close()
        await engine.dispose()
    return results


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--sizes", default="10000,50000,100000")
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args(argv)
    sizes = sorted(int(size) for size in args.sizes.split(","))
    results = asyncio.run(benchmark(sizes, args.repeats))
    print("| Chunks | Samples | p50 ms | p95 ms | Max ms |")
    print("|---:|---:|---:|---:|---:|")
    for row in results:
        print(
            f"| {row['chunks']:,} | {row['samples']} | {row['p50_ms']} | {row['p95_ms']} | "
            f"{row['max_ms']} |"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
