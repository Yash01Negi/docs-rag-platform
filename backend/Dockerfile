# syntax=docker/dockerfile:1
#
# Two images from one Dockerfile, sharing their dependency layers:
#   --target api     (default) FastAPI app and migrations
#   --target worker  ingestion worker
# Both install the optional 'embeddings' extra (CPU-only PyTorch, sentence-transformers):
# the worker embeds chunks and the API embeds search queries with the same model. Model
# weights are NOT baked in: they are downloaded at runtime into the model-cache volume.

FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_NO_CACHE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    STORAGE_DIR=/data/uploads

WORKDIR /app

# /data/uploads is created here so a new named volume mounted on it inherits the
# unprivileged user's ownership.
RUN useradd --create-home --uid 10001 appuser \
    && pip install uv==0.11.32 \
    && mkdir -p /data/uploads \
    && chown appuser:appuser /data/uploads

COPY pyproject.toml uv.lock README.md ./

ENV MODEL_CACHE_DIR=/models \
    HF_HOME=/models/huggingface

RUN mkdir -p /models && chown appuser:appuser /models

# Locked runtime dependencies plus the embedding extra, cached across code changes.
RUN uv sync --locked --no-dev --extra embeddings --no-install-project

COPY app ./app
RUN uv sync --locked --no-dev --extra embeddings --no-editable


FROM base AS worker

ENV WORKER_HEARTBEAT_FILE=/tmp/yashrag-worker.alive

USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-m", "app.worker", "healthcheck"]

CMD ["python", "-m", "app.worker", "run"]


FROM base AS api

COPY alembic.ini ./
COPY alembic ./alembic

USER appuser
EXPOSE 8000

HEALTHCHECK --interval=10s --timeout=3s --start-period=10s --retries=5 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v1/health', timeout=2)"]

CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000"]
