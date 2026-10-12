# YashRAG backend

FastAPI service and ingestion worker for YashRAG. See the [repository README](../README.md)
for setup, the [architecture notes](../docs/architecture.md) for how the pieces fit
together, and the [ingestion pipeline](../docs/ingestion-pipeline.md) for how documents are
processed.

```bash
uv sync                         # install locked dependencies (uv.lock) into .venv
cp ../.env.example ../.env      # then edit DATABASE_URL if needed
uv run alembic upgrade head
uv run uvicorn app.main:create_app --factory --reload
```

The ingestion worker needs the optional `embeddings` extra (sentence-transformers and
PyTorch):

```bash
uv sync --extra embeddings
uv run --extra embeddings python -m app.worker prepare-model  # optional: download BAAI/bge-m3 ahead of time
uv run --extra embeddings python -m app.worker run
```

Tests (`uv run pytest`) use a deterministic fake embedding provider and do not need the
extra. `RUN_MODEL_TESTS=1 uv run --extra embeddings pytest -m model` runs the real-model smoke test.
