"""Command-line entry point for the ingestion worker.

python -m app.worker run             # process ingestion jobs until SIGTERM/SIGINT
python -m app.worker prepare-model   # download and verify the embedding model
python -m app.worker healthcheck     # exit 0 if the worker's liveness file is fresh
python -m app.worker rebuild-search-index  # recompute keyword-search vectors of all chunks
"""

import argparse
import asyncio
import logging
import signal
import sys
import time

from app.core.config import get_settings
from app.core.logging import configure_logging
from app.db.session import create_engine
from app.embeddings import create_embedding_provider
from app.ingestion.errors import IngestionError
from app.storage import LocalFileStorage
from app.worker.runner import Worker

logger = logging.getLogger("app.worker")

# The liveness file is rewritten at least every 10 seconds while the event loop is healthy.
HEALTHCHECK_MAX_AGE_SECONDS = 60


async def _run() -> None:
    settings = get_settings()
    engine = create_engine(settings)
    worker = Worker(
        settings,
        engine,
        create_embedding_provider(settings),
        LocalFileStorage(settings.storage_dir),
    )
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, worker.stop)
    try:
        await worker.run()
    finally:
        await engine.dispose()


def _prepare_model() -> int:
    settings = get_settings()
    provider = create_embedding_provider(settings)
    try:
        info = provider.load()
        vectors = provider.embed(["YashRAG model check.", "モデルの動作確認です。"])
    except IngestionError as exc:
        logger.error("model_prepare_failed", extra={"error_code": exc.code.value})
        print(f"error: {exc.message}", file=sys.stderr)
        return 1
    print(f"model:     {info.name}")
    print(f"revision:  {info.revision}")
    print(f"requested: {settings.embedding_model_revision}  (EMBEDDING_MODEL_REVISION)")
    print(f"dimension: {info.dimension}")
    print(f"device:    {getattr(provider, 'device', 'n/a')}")
    print(f"check:     embedded {len(vectors)} texts, {len(vectors[0])} dimensions each")
    return 0


def _rebuild_search_index() -> int:
    from sqlalchemy import create_engine as create_sync_engine

    from app.search.lexical_index import rebuild_lexical_index

    engine = create_sync_engine(get_settings().database_url.get_secret_value())
    try:
        with engine.begin() as connection:
            count = rebuild_lexical_index(connection)
    finally:
        engine.dispose()
    print(f"rebuilt keyword-search vectors for {count} chunks")
    return 0


def _healthcheck() -> int:
    path = get_settings().worker_heartbeat_file
    if path is None:
        print("WORKER_HEARTBEAT_FILE is not set", file=sys.stderr)
        return 1
    try:
        timestamp = float(path.read_text().split()[0])
    except (OSError, ValueError, IndexError):
        print("no liveness file yet", file=sys.stderr)
        return 1
    age = time.time() - timestamp
    if age > HEALTHCHECK_MAX_AGE_SECONDS:
        print(f"liveness file is {age:.0f}s old", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.worker")
    parser.add_argument(
        "command",
        nargs="?",
        default="run",
        choices=["run", "prepare-model", "healthcheck", "rebuild-search-index"],
    )
    args = parser.parse_args(argv)
    if args.command == "healthcheck":
        return _healthcheck()

    configure_logging(get_settings().log_level)
    if args.command == "prepare-model":
        return _prepare_model()
    if args.command == "rebuild-search-index":
        return _rebuild_search_index()
    asyncio.run(_run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
