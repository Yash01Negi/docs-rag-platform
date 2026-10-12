"""Run the retrieval evaluation against a throwaway database.

    uv run python -m evaluation.run --reset --modes keyword
    uv run --extra embeddings python -m evaluation.run --reset   # all modes, real BGE-M3

The harness ingests the dataset documents through the real worker pipeline (extraction,
chunking, embedding, keyword index), runs every query through SearchService in each mode,
and writes results.json and report.md. It reads DATABASE_URL and the usual settings from
the environment; the database must already be migrated (alembic upgrade head).

--reset deletes every document in the target database first. It refuses to do so unless
the database name ends in _eval or _test.

With EMBEDDING_PROVIDER=fake (ENVIRONMENT=test) the semantic and hybrid modes would rank by
hash-based vectors that carry no meaning, so only keyword mode runs unless
--allow-fake-embeddings is given, and the report then says the semantic numbers are fake.
"""

import argparse
import asyncio
import hashlib
import json
import os
import platform
import subprocess
import sys
import tempfile
import time
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from app.core.config import Settings, get_settings
from app.db.models import Chunk, Document, DocumentStatus, JobKind
from app.db.session import create_engine, create_session_factory
from app.embeddings import create_embedding_provider
from app.embeddings.base import EmbeddingProvider
from app.ingestion import jobs
from app.ingestion.validation import file_type_from_extension
from app.search.retrieval import SearchFilters
from app.search.service import SearchMode, SearchRequest, SearchService
from app.storage import LocalFileStorage, new_storage_key
from app.worker.runner import Worker
from evaluation.dataset import Dataset, Query, load_dataset, normalize_for_matching
from evaluation.metrics import mean, percentile, query_metrics

CUTOFFS = (5, 10)
SLICES = ("all", "en", "ja", "mixed")


class EvaluationError(RuntimeError):
    pass


@dataclass
class Retrieved:
    rank: int
    document: str
    chunk_ordinal: int
    score: float
    passages: list[int]


@dataclass
class QueryRun:
    query_id: str
    mode: str
    retrieved: list[Retrieved]
    latencies_ms: list[float]
    warnings: list[str]
    metrics: dict[str, float] = field(default_factory=dict)


# ------------------------------------------------------------------------- ingestion


async def reset_database(engine: AsyncEngine) -> None:
    database = make_url(str(engine.url)).database or ""
    if not database.endswith(("_eval", "_test")):
        raise EvaluationError(
            f"refusing to delete documents from database {database!r}: "
            "--reset only works on a database whose name ends in _eval or _test"
        )
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE documents CASCADE"))


async def ingest(
    dataset: Dataset,
    settings: Settings,
    engine: AsyncEngine,
    provider: EmbeddingProvider,
    storage_dir: Path,
) -> dict[uuid.UUID, str]:
    """Upload every dataset document and run the worker until all are indexed."""
    storage = LocalFileStorage(storage_dir)
    sessions = create_session_factory(engine)
    names: dict[uuid.UUID, str] = {}
    async with sessions() as session:
        if await session.scalar(select(Document.id).limit(1)) is not None:
            raise EvaluationError("the database already holds documents; run with --reset")
        for name, path in dataset.documents.items():
            key = new_storage_key()
            storage.put_file(key, path)
            document = Document(
                id=uuid.uuid4(),
                filename=name,
                file_type=file_type_from_extension(name).value,
                mime_type="text/markdown" if name.endswith(".md") else "text/plain",
                size_bytes=path.stat().st_size,
                sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
                storage_key=key,
                status=DocumentStatus.PENDING.value,
                chunk_count=0,
            )
            session.add(document)
            await session.flush()
            await jobs.enqueue_job(
                session, document, JobKind.INGEST, max_attempts=1, request_id=None
            )
            names[document.id] = name
        await session.commit()

    worker = Worker(settings, engine, provider, storage, worker_id="evaluation")
    while await worker.run_once():
        await worker.wait_idle()
    await worker.wait_idle()

    async with sessions() as session:
        rows = (await session.execute(select(Document.filename, Document.status))).all()
    failed = sorted(name for name, status in rows if status != DocumentStatus.COMPLETED.value)
    if failed:
        raise EvaluationError(f"documents failed to index: {', '.join(failed)}")
    return names


@dataclass(frozen=True)
class IndexedChunk:
    document: str
    ordinal: int
    normalized_text: str


async def load_index(
    sessions: async_sessionmaker[AsyncSession], names: dict[uuid.UUID, str]
) -> dict[uuid.UUID, IndexedChunk]:
    async with sessions() as session:
        rows = (
            await session.execute(select(Chunk.id, Chunk.document_id, Chunk.ordinal, Chunk.text))
        ).all()
    return {
        row.id: IndexedChunk(names[row.document_id], row.ordinal, normalize_for_matching(row.text))
        for row in rows
    }


def passages_in(chunk: IndexedChunk, query: Query) -> list[int]:
    return [
        index
        for index, passage in enumerate(query.relevant)
        if passage.doc == chunk.document
        and normalize_for_matching(passage.anchor) in chunk.normalized_text
    ]


def check_anchors_indexed(dataset: Dataset, index: dict[uuid.UUID, IndexedChunk]) -> None:
    """Every judged passage must sit whole inside at least one chunk, or it can't be found."""
    for query in dataset.queries:
        for position, passage in enumerate(query.relevant):
            if not any(position in passages_in(chunk, query) for chunk in index.values()):
                raise EvaluationError(
                    f"query {query.id}: passage {passage.anchor!r} is split across chunks; "
                    "shorten the anchor"
                )


# ---------------------------------------------------------------------------- search


async def run_queries(
    dataset: Dataset,
    service: SearchService,
    sessions: async_sessionmaker[AsyncSession],
    index: dict[uuid.UUID, IndexedChunk],
    modes: Sequence[SearchMode],
    repeats: int,
) -> list[QueryRun]:
    top_k = max(CUTOFFS)
    runs: list[QueryRun] = []
    for mode in modes:
        # One untimed search per mode so connection setup and caches don't skew latency.
        async with sessions() as session:
            await service.search(session, _request(dataset.queries[0].text, mode, top_k))
        for query in dataset.queries:
            latencies: list[float] = []
            outcome = None
            for _ in range(repeats):
                async with sessions() as session:
                    started = time.perf_counter()
                    outcome = await service.search(session, _request(query.text, mode, top_k))
                    latencies.append((time.perf_counter() - started) * 1000)
            assert outcome is not None  # noqa: S101 - repeats >= 1 is checked by the CLI
            retrieved = [
                Retrieved(
                    rank=hit.rank,
                    document=hit.chunk.filename,
                    chunk_ordinal=hit.chunk.ordinal,
                    score=hit.score,
                    passages=passages_in(index[hit.chunk.chunk_id], query),
                )
                for hit in outcome.hits
            ]
            run = QueryRun(query.id, mode.value, retrieved, latencies, list(outcome.warnings))
            if query.relevant:
                ranking = [frozenset(item.passages) for item in retrieved]
                grades = [passage.grade for passage in query.relevant]
                for k in CUTOFFS:
                    values = query_metrics(ranking, grades, k)
                    run.metrics[f"recall@{k}"] = values.recall
                    run.metrics[f"precision@{k}"] = values.precision
                    run.metrics[f"mrr@{k}"] = values.reciprocal_rank
                    run.metrics[f"ndcg@{k}"] = values.ndcg
            runs.append(run)
    return runs


def _request(query: str, mode: SearchMode, top_k: int) -> SearchRequest:
    # The API applies NFC to queries before searching; do the same here.
    return SearchRequest(
        query=unicodedata.normalize("NFC", query),
        mode=mode,
        top_k=top_k,
        filters=SearchFilters(),
    )


# --------------------------------------------------------------------------- summary


def summarize(dataset: Dataset, runs: list[QueryRun], modes: Sequence[SearchMode]) -> dict:  # type: ignore[type-arg]
    queries = {query.id: query for query in dataset.queries}
    summary: dict[str, dict] = {}  # type: ignore[type-arg]
    for mode in modes:
        mode_runs = [run for run in runs if run.mode == mode.value]
        judged = [run for run in mode_runs if queries[run.query_id].relevant]
        by_slice: dict[str, dict[str, float | int]] = {}
        for name in SLICES:
            selected = [
                run for run in judged if name == "all" or queries[run.query_id].language == name
            ]
            if selected:
                by_slice[name] = _averages(selected)
        by_type: dict[str, dict[str, float | int]] = {}
        for query_type in sorted({t for q in dataset.queries for t in q.types} - {"no_relevant"}):
            selected = [run for run in judged if query_type in queries[run.query_id].types]
            if selected:
                by_type[query_type] = _averages(selected)
        unanswerable = [run for run in mode_runs if not queries[run.query_id].relevant]
        latencies = [value for run in mode_runs for value in run.latencies_ms]
        summary[mode.value] = {
            "by_language": by_slice,
            "by_type": by_type,
            "no_relevant": {
                "queries": len(unanswerable),
                "empty_results": sum(1 for run in unanswerable if not run.retrieved),
                "mean_results": mean([len(run.retrieved) for run in unanswerable])
                if unanswerable
                else 0.0,
            },
            "latency_ms": {
                "samples": len(latencies),
                "p50": percentile(latencies, 50),
                "p95": percentile(latencies, 95),
            },
        }
    return summary


def _averages(runs: list[QueryRun]) -> dict[str, float | int]:
    names = runs[0].metrics.keys()
    averages: dict[str, float | int] = {
        name: mean([run.metrics[name] for run in runs]) for name in names
    }
    averages["queries"] = len(runs)
    return averages


# ---------------------------------------------------------------------------- report


def render_report(result: dict) -> str:  # type: ignore[type-arg]
    meta = result["metadata"]
    lines = [
        "# Retrieval evaluation report",
        "",
        f"- Run at: {meta['run_at']} (git {meta['git_commit']})",
        f"- Dataset: version {meta['dataset_version']}, {meta['documents']} documents, "
        f"{meta['queries']} queries, sha256 {meta['dataset_sha256'][:12]}",
        f"- Embedding provider: {meta['embedding']['provider']} "
        f"({meta['embedding']['model']} @ {meta['embedding']['revision']}, "
        f"device {meta['embedding']['device']})",
        f"- Chunks indexed: {meta['chunks']} (target {meta['chunking']['target_tokens']} "
        f"tokens, overlap {meta['chunking']['overlap_tokens']}, "
        f"min {meta['chunking']['min_tokens']})",
        f"- Search: BM25 k1={meta['search']['bm25_k1']} b={meta['search']['bm25_b']}, "
        f"RRF k={meta['search']['rrf_k']}, fusion depth {meta['search']['fusion_depth']}",
        f"- Host: {meta['host']['platform']}, Python {meta['host']['python']}, "
        f"{meta['host']['cpus']} CPUs; latency over {meta['repeats']} runs per query",
        "",
    ]
    if meta["fake_embeddings"] and set(meta["modes"]) - {"keyword"}:
        lines += [
            "> **Fake embeddings.** Semantic and hybrid numbers below come from hash-based test",
            "> vectors with no meaning. They test the harness, not retrieval quality.",
            "",
        ]
    elif meta["fake_embeddings"]:
        lines += [
            "> Keyword mode only. Keyword search does not use embeddings, so the test vectors",
            "> stored at indexing time do not affect these numbers. Semantic and hybrid",
            "> results need the real model.",
            "",
        ]
    summary = result["summary"]
    modes = list(summary)
    lines += ["## Quality by query language", ""]
    header = "| Mode | Slice | Queries | Recall@5 | Recall@10 | Precision@5 | MRR@10 | nDCG@10 |"
    lines += [header, "|---|---|---:|---:|---:|---:|---:|---:|"]
    for mode in modes:
        for name, values in summary[mode]["by_language"].items():
            lines.append(
                f"| {mode} | {name} | {values['queries']} | {values['recall@5']:.3f} | "
                f"{values['recall@10']:.3f} | {values['precision@5']:.3f} | "
                f"{values['mrr@10']:.3f} | {values['ndcg@10']:.3f} |"
            )
    lines += ["", "## Quality by query type", ""]
    types = sorted({t for mode in modes for t in summary[mode]["by_type"]})
    lines += [
        "| Query type | Queries | "
        + " | ".join(f"{m} nDCG@10" for m in modes)
        + " | "
        + " | ".join(f"{m} Recall@10" for m in modes)
        + " |",
        "|---|---:|" + "---:|" * (2 * len(modes)),
    ]
    for query_type in types:
        rows = [summary[m]["by_type"].get(query_type) for m in modes]
        count = next(row["queries"] for row in rows if row)
        lines.append(
            f"| {query_type} | {count} | "
            + " | ".join(f"{row['ndcg@10']:.3f}" if row else "-" for row in rows)
            + " | "
            + " | ".join(f"{row['recall@10']:.3f}" if row else "-" for row in rows)
            + " |"
        )
    lines += [
        "",
        "## Queries with no relevant passage",
        "",
        "The ideal answer is an empty list. Semantic search always returns its nearest",
        "neighbours unless min_similarity is set, so it cannot return nothing here.",
        "",
        "| Mode | Queries | Empty results | Mean results returned |",
        "|---|---:|---:|---:|",
    ]
    for mode in modes:
        values = summary[mode]["no_relevant"]
        lines.append(
            f"| {mode} | {values['queries']} | {values['empty_results']} | "
            f"{values['mean_results']:.1f} |"
        )
    lines += [
        "",
        "## Latency",
        "",
        "Wall time of SearchService.search in process (query analysis, query embedding,",
        "database queries, snippets); HTTP overhead is not included.",
        "",
        "| Mode | Samples | p50 ms | p95 ms |",
        "|---|---:|---:|---:|",
    ]
    for mode in modes:
        values = summary[mode]["latency_ms"]
        lines.append(
            f"| {mode} | {values['samples']} | {values['p50']:.1f} | {values['p95']:.1f} |"
        )
    lines += [
        "",
        "## Per query",
        "",
        "Rank of the first chunk containing a judged passage (- = not in the top 10; for",
        "no-relevant queries, the number of results returned).",
        "",
        "| Query | Lang | Text | " + " | ".join(modes) + " |",
        "|---|---|---|" + "---:|" * len(modes),
    ]
    runs = {(run["query_id"], run["mode"]): run for run in result["runs"]}
    for query in result["queries"]:
        cells = []
        for mode in modes:
            run = runs[(query["id"], mode)]
            if not query["relevant"]:
                cells.append(f"{len(run['retrieved'])} results")
                continue
            first = next((item["rank"] for item in run["retrieved"] if item["passages"]), None)
            cells.append(str(first) if first else "-")
        query_text = query["text"].replace("|", "\\|")
        lines.append(
            f"| {query['id']} | {query['language']} | {query_text} | " + " | ".join(cells) + " |"
        )
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------------------- CLI


def _git_commit() -> str:
    try:
        return subprocess.run(  # noqa: S603
            ["git", "rev-parse", "--short", "HEAD"],  # noqa: S607
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


async def evaluate(
    settings: Settings,
    modes: Sequence[SearchMode],
    repeats: int,
    reset: bool,
    allow_fake: bool,
    dataset: Dataset | None = None,
) -> dict:  # type: ignore[type-arg]
    dataset = dataset or load_dataset()
    fake = settings.embedding_provider == "fake"
    if fake and not allow_fake and any(mode is not SearchMode.KEYWORD for mode in modes):
        raise EvaluationError(
            "EMBEDDING_PROVIDER=fake gives meaningless semantic rankings; run --modes keyword, "
            "use the real model, or pass --allow-fake-embeddings to test the harness"
        )
    settings = settings.model_copy(
        update={
            "chunk_target_tokens": dataset.chunking.target_tokens,
            "chunk_overlap_tokens": dataset.chunking.overlap_tokens,
            "chunk_min_tokens": dataset.chunking.min_tokens,
        }
    )
    engine = create_engine(settings)
    provider = create_embedding_provider(settings)
    service = SearchService(settings, provider)
    try:
        if reset:
            await reset_database(engine)
        with tempfile.TemporaryDirectory(prefix="yashrag-eval-") as storage_dir:
            names = await ingest(dataset, settings, engine, provider, Path(storage_dir))
        sessions = create_session_factory(engine)
        index = await load_index(sessions, names)
        check_anchors_indexed(dataset, index)
        await service.warm_up()
        runs = await run_queries(dataset, service, sessions, index, modes, repeats)
        model = provider.load()
    finally:
        service.close()
        await engine.dispose()

    result = {
        "metadata": {
            "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "git_commit": _git_commit(),
            "dataset_version": dataset.version,
            "dataset_sha256": dataset.sha256,
            "documents": len(dataset.documents),
            "queries": len(dataset.queries),
            "chunks": len(index),
            "chunking": asdict(dataset.chunking),
            "embedding": {
                "provider": settings.embedding_provider,
                "model": model.name,
                "revision": model.revision,
                "dimension": model.dimension,
                "device": getattr(provider, "device", "n/a"),
            },
            "fake_embeddings": fake,
            "search": {
                "bm25_k1": settings.search_bm25_k1,
                "bm25_b": settings.search_bm25_b,
                "rrf_k": settings.search_rrf_k,
                "fusion_depth": settings.search_fusion_depth,
            },
            "modes": [mode.value for mode in modes],
            "repeats": repeats,
            "host": {
                "platform": platform.platform(),
                "python": platform.python_version(),
                "cpus": os.cpu_count(),
            },
        },
        "summary": summarize(dataset, runs, modes),
        "queries": [
            {
                "id": query.id,
                "text": query.text,
                "language": query.language,
                "types": list(query.types),
                "relevant": [asdict(passage) for passage in query.relevant],
            }
            for query in dataset.queries
        ],
        "runs": [asdict(run) for run in runs],
    }
    return result


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--modes",
        default="keyword,semantic,hybrid",
        help="comma-separated search modes to evaluate (default: all three)",
    )
    parser.add_argument("--repeats", type=int, default=3, help="timed runs per query (default 3)")
    parser.add_argument("--output-dir", type=Path, default=Path("evaluation/results/latest"))
    parser.add_argument(
        "--reset", action="store_true", help="delete all documents first (_eval/_test DBs only)"
    )
    parser.add_argument("--allow-fake-embeddings", action="store_true")
    args = parser.parse_args(argv)
    try:
        modes = [SearchMode(mode.strip()) for mode in args.modes.split(",") if mode.strip()]
    except ValueError as exc:
        parser.error(str(exc))
    if not modes or args.repeats < 1:
        parser.error("give at least one mode and --repeats of 1 or more")

    try:
        result = asyncio.run(
            evaluate(get_settings(), modes, args.repeats, args.reset, args.allow_fake_embeddings)
        )
    except EvaluationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report = render_report(result)
    (args.output_dir / "report.md").write_text(report, encoding="utf-8")
    print(report)
    print(f"wrote {args.output_dir}/results.json and report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
