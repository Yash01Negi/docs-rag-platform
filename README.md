# YashRAG

**A bilingual (Japanese/English) document intelligence platform built on
retrieval-augmented generation (RAG).**

YashRAG is being built in phases. The goal is a system where you can upload Japanese and
English documents, ask questions in either language, and get answers grounded in those
documents with citations, backed by an evaluation suite that measures retrieval and answer
quality.

> **Status: Phase 2 (Ingestion) is implemented.** You can upload Japanese, English and
> mixed PDF, Markdown and text files; a background worker extracts, chunks and embeds them
> with a locally run BAAI/bge-m3 model into pgvector, and a Documents UI manages them.
> **Nothing reads the index yet:** retrieval, chat, citations and evaluation are not
> implemented, and are marked as planned in the UI.
>
> **Not verified:** real BGE-M3 inference could not be run in the environment where Phase 2
> was built (the model download was blocked). The pipeline was tested end to end with a
> deterministic test-only embedding provider, and the real-model code path with a small
> stand-in model. See [ADR 0003](docs/decisions/0003-local-bge-m3.md) and the opt-in
> [real-model smoke test](#real-model-smoke-test).

## Implementation status

| Area | Status |
| --- | --- |
| FastAPI backend with `/api/v1/health` (liveness) and `/api/v1/ready` (database readiness) | ✅ Implemented |
| Environment-based configuration (pydantic-settings), no default credentials | ✅ Implemented |
| Structured JSON logging with per-request IDs | ✅ Implemented |
| Consistent JSON error responses | ✅ Implemented |
| CORS with an explicit origin allowlist | ✅ Implemented |
| PostgreSQL 17 + pgvector, Alembic migrations | ✅ Implemented |
| React + TypeScript + Tailwind application shell with live backend status | ✅ Implemented |
| Docker Compose development stack with health checks | ✅ Implemented |
| GitHub Actions CI (backend and frontend lint, tests, type check, build, compose validation) | ✅ Implemented |
| Document upload with content validation, 20 MB limit and SHA-256 deduplication (PDF, Markdown, text) | ✅ Implemented |
| Text extraction with page numbers and headings (PyMuPDF for PDF) | ✅ Implemented |
| Japanese/English/mixed language detection and token-based multilingual chunking | ✅ Implemented |
| Local BAAI/bge-m3 dense embeddings (1024-d) stored in pgvector | ✅ Implemented, ⚠️ real model unverified |
| Database-backed ingestion worker (leases, retries, recovery, graceful shutdown) | ✅ Implemented |
| Document management API (upload, list, details, chunks, file, retry, reindex, delete) | ✅ Implemented |
| Documents UI (drag-and-drop upload, library, details, status polling) | ✅ Implemented |
| Frontend unit tests (Vitest + Testing Library) | ✅ Implemented |
| OCR for scanned PDFs | ❌ Not planned yet |
| Approximate nearest-neighbour (HNSW/IVFFlat) index | 🕒 Planned with retrieval |
| Hybrid retrieval (vector + keyword) and reranking | 🕒 Planned |
| Chat with grounded answers and citations | 🕒 Planned |
| Retrieval and answer-quality evaluation | 🕒 Planned |
| Authentication | 🕒 Planned |
| AWS deployment | 🕒 Planned |

## Technology stack

| Layer | Technology |
| --- | --- |
| Backend | Python 3.12, FastAPI, Pydantic v2, pydantic-settings, Uvicorn, PyMuPDF |
| Embeddings (worker only) | BAAI/bge-m3 via sentence-transformers and PyTorch (CPU, or Apple MPS on the host) |
| Database | PostgreSQL 17, pgvector 0.8.1, SQLAlchemy 2 (async), psycopg 3, Alembic |
| Frontend | React 19, TypeScript, Vite, Tailwind CSS v4, React Router |
| Tooling | uv (locked Python dependencies), Ruff (lint + format), pytest, oxlint, Vitest, Docker Compose, GitHub Actions |

## Architecture overview

```mermaid
flowchart LR
    browser([Browser]) --> fe["Frontend<br/>React + Vite"]
    fe -- "/api/*" --> be["API<br/>FastAPI"]
    be --> db[("PostgreSQL<br/>+ pgvector")]
    be -- "store original" --> files[("uploads<br/>volume")]
    worker["Ingestion worker<br/>extract · chunk · BGE-M3"] -- "claim jobs · write chunks" --> db
    files --> worker
    cache[("model cache<br/>volume")] --> worker
    mig["Alembic<br/>migrations"] --> db
```

- The **frontend** has a working Documents area (upload, library, details) and a dashboard
  with live backend status. Chat and Evaluation are labelled placeholders.
- The **API** validates and stores uploads, queues ingestion jobs in PostgreSQL and serves
  document data. It never loads the embedding model.
- The **worker** is a separate process that claims jobs, extracts text, chunks it, embeds it
  with BAAI/bge-m3 and writes chunks and vectors in one transaction.
- The **database** is PostgreSQL with pgvector
  ([ADR 0002](docs/decisions/0002-postgres-pgvector.md)); it is also the job queue, so
  there is no Redis or Celery.

More detail: [docs/architecture.md](docs/architecture.md) ·
[docs/ingestion-pipeline.md](docs/ingestion-pipeline.md) ·
Decisions: [docs/decisions/](docs/decisions/)

```text
docs-rag-platform/
├── backend/             FastAPI app, ingestion worker, Alembic migrations, tests
├── frontend/            React + TypeScript + Vite app
├── docs/                Architecture notes and decision records
├── infra/               Deployment configuration (planned)
├── .github/workflows/   CI
├── docker-compose.yml   Local development stack
└── .env.example         Configuration template
```

## Getting started

### Option A: Docker Compose (recommended)

Requirements: Docker with the Compose plugin.

```bash
git clone https://github.com/Yash01Negi/docs-rag-platform.git
cd docs-rag-platform
cp .env.example .env            # local-only defaults; edit the password if you like
docker compose up --build
```

Compose starts the database, applies migrations (a one-off `migrate` container), then starts
the API, the ingestion worker and the frontend once their dependencies are healthy.

The worker downloads BAAI/bge-m3 (about 2.3 GB) into the `model-cache` volume the first time
it processes a document. To download it ahead of time, and to see the exact model revision
it resolved (useful for pinning `EMBEDDING_MODEL_REVISION`):

```bash
docker compose run --rm worker python -m app.worker prepare-model
```

The worker image is large (several GB): it contains PyTorch, and on Linux x86_64 the default
PyTorch wheel pulls in CUDA libraries even though the container runs on CPU. The model
weights are never part of the image. The worker container is limited to 4 CPUs and 6 GB of
memory by default (`WORKER_CPUS`, `WORKER_MEMORY`); BGE-M3 on CPU needs roughly 4 to 6 GB.

| Service | URL |
| --- | --- |
| Frontend | <http://localhost:5173> |
| API liveness | <http://localhost:8000/api/v1/health> |
| API readiness | <http://localhost:8000/api/v1/ready> |
| Documents UI | <http://localhost:5173/documents> |
| OpenAPI docs | <http://localhost:8000/api/v1/docs> |

Stop with `docker compose down`. Add `-v` to also delete the database, uploaded files and
the model cache.

### Option B: Run the backend and frontend on your machine

Requirements: [uv](https://docs.astral.sh/uv/) (it can install Python 3.12 for you),
Node.js 22+, and a PostgreSQL server with pgvector. The simplest
way to get the database is to start only that service from Compose:

```bash
cp .env.example .env
docker compose up -d db
```

Backend (the root `.env` is read automatically):

```bash
cd backend
uv sync                         # creates .venv from uv.lock, including dev tools
uv run alembic upgrade head
uv run uvicorn app.main:create_app --factory --reload
```

Ingestion worker (in another terminal). It needs the optional `embeddings` extra:

```bash
cd backend
uv sync --extra embeddings      # adds sentence-transformers and PyTorch
uv run --extra embeddings python -m app.worker prepare-model  # optional: download the model now
uv run --extra embeddings python -m app.worker run
```

Frontend (in another terminal):

```bash
cd frontend
npm ci
npm run dev                     # http://localhost:5173, proxies /api to localhost:8000
```

### Apple Silicon: run the worker on the host

Docker Desktop on macOS cannot use the Apple GPU, so the worker container runs BGE-M3 on
CPU. To let the model use Metal (MPS) on an M-series Mac, run the API and the worker
natively (Option B) and keep only the database in Docker:

```bash
cp .env.example .env
docker compose up -d db
cd backend
uv sync --extra embeddings
uv run alembic upgrade head
uv run uvicorn app.main:create_app --factory --reload   # terminal 1
EMBEDDING_DEVICE=auto uv run --extra embeddings python -m app.worker run  # terminal 2
```

Both processes must see the same uploaded files, which they do with the default
`STORAGE_DIR=data/uploads` when started from the same directory. (The API container's
`uploads` volume lives inside the Docker VM on macOS and is not reachable from a host
worker.) The worker's `embedding_model_loaded` log line shows which device it picked. MPS
support depends on your PyTorch build, and no performance figures are claimed here.

### Configuration

All configuration is read from environment variables (or `.env`). See
[`.env.example`](.env.example) for the full list.

| Variable | Required | Description |
| --- | --- | --- |
| `DATABASE_URL` | yes | `postgresql+psycopg://user:password@host:port/db`. No default. |
| `CORS_ALLOWED_ORIGINS` | no | Comma-separated origins, e.g. `http://localhost:5173`. `*` is rejected. Default: none. |
| `ENVIRONMENT` | no | `development`, `test` or `production`. Default `development`. |
| `LOG_LEVEL` | no | `DEBUG`, `INFO`, `WARNING`, `ERROR`. Default `INFO`. |
| `POSTGRES_USER` / `POSTGRES_PASSWORD` / `POSTGRES_DB` | Compose only | Credentials for the local database container. |
| `MAX_UPLOAD_BYTES` | no | Largest accepted upload. Default `20971520` (20 MiB). |
| `STORAGE_DIR` | no | Where original files are stored. Default `data/uploads`; `/data/uploads` (a named volume) in Docker. |
| `EMBEDDING_PROVIDER` | no | `bge-m3` (default). `fake` is a deterministic test double and is refused unless `ENVIRONMENT=test`. |
| `EMBEDDING_MODEL_NAME` | no | Hugging Face model ID. Default `BAAI/bge-m3`. |
| `EMBEDDING_MODEL_REVISION` | no | Branch, tag or commit SHA. Default `main`; pin a SHA for reproducible indexes. |
| `EMBEDDING_DIMENSION` | no | Must be `1024` (the `vector(1024)` column). |
| `EMBEDDING_DEVICE` | no | `auto` (MPS if available, else CPU), `cpu`, `mps` or `cuda`. Default `auto`; Compose uses `cpu`. |
| `EMBEDDING_BATCH_SIZE` | no | Chunks per model call. Default `16`; Compose uses `8`. |
| `EMBEDDING_MAX_SEQ_LENGTH` | no | Longest model input in tokens; longer inputs are rejected, never truncated. Default `1024`. |
| `MODEL_CACHE_DIR` | no | Model weights cache. Default `data/models`; `/models` (a named volume) in Docker. |
| `EMBEDDING_LOCAL_FILES_ONLY` | no | `true` forbids network access to the Hugging Face Hub. Default `false`. |
| `CHUNK_TARGET_TOKENS` / `CHUNK_OVERLAP_TOKENS` / `CHUNK_MIN_TOKENS` | no | Chunk size, overlap and minimum tail, in model tokens. Defaults `400` / `60` / `40`. |
| `WORKER_CONCURRENCY` | no | Jobs processed at once per worker. Default `1`. |
| `WORKER_MAX_ATTEMPTS` | no | Attempts per job for retryable errors. Default `3`. |
| `WORKER_LEASE_SECONDS` / `WORKER_HEARTBEAT_SECONDS` | no | Job lease length and renewal interval. Defaults `120` / `15`. |
| `WORKER_RETRY_BACKOFF_SECONDS` | no | First retry delay, doubled per attempt, capped at 1 hour. Default `30`. |
| `WORKER_POLL_INTERVAL_SECONDS` | no | Idle polling interval. Default `2`. |
| `WORKER_SHUTDOWN_GRACE_SECONDS` | no | How long in-flight jobs may finish after SIGTERM. Default `30`. |
| `WORKER_HEARTBEAT_FILE` | no | File touched by the worker for health checks. Set in the Docker image. |
| `WORKER_PRELOAD_MODEL` | no | Load the model at startup instead of on the first job. Default `false`. |
| `WORKER_CPUS` / `WORKER_MEMORY` / `WORKER_TORCH_THREADS` | Compose only | Worker container CPU and memory limits and PyTorch threads. Defaults `4` / `6g` / `4`. |

## Testing

Backend dependencies are pinned in [`backend/uv.lock`](backend/uv.lock). From `backend/`:

```bash
uv sync                         # install the locked versions
uv run ruff check .             # lint
uv run ruff format --check .    # formatting
uv run pytest                   # unit tests; integration tests are skipped without a database
```

To also run the integration tests (readiness, migrations, the document API, the job queue
and the worker against a real database), point `TEST_DATABASE_URL` at a PostgreSQL + pgvector server that has
been migrated. The test user needs permission to create databases, because some tests create
and drop a temporary database.

```bash
export TEST_DATABASE_URL="postgresql+psycopg://yashrag:change-me-local-dev-only@localhost:5432/yashrag"
uv run alembic upgrade head
uv run pytest
```

Tests use a deterministic fake embedding provider (`EMBEDDING_PROVIDER=fake`, only allowed
with `ENVIRONMENT=test`), so the normal suite needs neither PyTorch nor the model.

#### Real-model smoke test

`tests/test_bge_m3_smoke.py` loads the real BAAI/bge-m3 and checks dimensions,
normalization, determinism and that a Japanese sentence lands closer to its English
translation than to an unrelated sentence. It is skipped unless enabled, needs the
`embeddings` extra and downloads about 2.3 GB on first run:

```bash
uv sync --extra embeddings
RUN_MODEL_TESTS=1 uv run --extra embeddings pytest -m model
```

Frontend (from `frontend/`):

```bash
npm run lint                    # oxlint (React, TypeScript and accessibility rules)
npm run typecheck               # TypeScript (tsc -b)
npm test                        # Vitest + Testing Library (jsdom)
npm run build                   # production build into dist/
```

CI runs all of the above (except the real-model smoke test) on every push to `main` and
every pull request, plus `alembic check` and Docker Compose config validation.

## Current limitations

- **No retrieval, chat, citations or evaluation.** Documents are indexed, but nothing
  searches the vectors yet, and there is no approximate nearest-neighbour index.
- **Real BGE-M3 output is unverified** in the environment where Phase 2 was built; see the
  status note above.
- No OCR: scanned PDFs fail with `no_extractable_text`. Only PDF, Markdown and plain text.
- Dense vectors only; no sparse or multi-vector (ColBERT) retrieval.
- No authentication or authorization, and therefore no per-user documents: anyone who can
  reach the API can upload, read and delete every document, and deduplication is global.
  Do not expose the stack to the public internet.
- Original files are stored on local disk (a Docker volume) with no backups. The storage
  interface is ready for object storage, but no S3 implementation exists.
- **PyMuPDF is licensed under AGPL-3.0** (or a commercial license from Artifex). Running a
  modified version of this project as a network service may carry AGPL obligations; check
  before deploying it publicly or commercially.
- The Docker setup is for local development only (Vite dev server, `uvicorn --reload`,
  source mounts). The worker image is large because of PyTorch.
- The UI is in English only.

## Roadmap

| Phase | Scope | Status |
| --- | --- | --- |
| 1. Foundation | Monorepo, FastAPI, PostgreSQL + pgvector, migrations, app shell, Docker, CI | ✅ Done |
| 2. Ingestion | Document upload, JA/EN parsing and chunking, local BGE-M3 embeddings, worker, Documents UI | ✅ Implemented (real model unverified) |
| 3. Retrieval | Vector + keyword hybrid search, ANN index, reranking, retrieval API | 🕒 Next |
| 4. Chat | Grounded answer generation with citations, chat UI | 🕒 Planned |
| 5. Evaluation | Bilingual evaluation set, retrieval and answer-quality metrics, evaluation UI | 🕒 Planned |
| 6. Deployment | Production images, AWS infrastructure, monitoring | 🕒 Planned |

## License

Released under the [MIT License](LICENSE).
