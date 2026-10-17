# YashRAG

**A bilingual (Japanese/English) document intelligence platform built on
retrieval-augmented generation (RAG).**

YashRAG is being built in phases. The goal is a system where you can upload Japanese and
English documents, ask questions in either language, and get answers grounded in those
documents with citations, backed by an evaluation suite that measures retrieval and answer
quality.

> **Status: Phase 4.1 (multi-model chat) is implemented.** You can upload Japanese, English
> and mixed PDF, Markdown and text files, search them (semantic, keyword or hybrid), and ask
> questions on the Chat page: answers stream in from the model you pick (Anthropic Claude,
> Google Gemini, or a local model through Ollama), grounded in retrieved passages, with
> citations that open the document at the cited passage. Conversations are stored and can
> be continued, renamed and deleted, and the model can change between questions.
>
> **Measured, not assumed:** retrieval was evaluated with the real model on 51 judged
> queries (semantic nDCG@10 0.927, the default; [docs/evaluation/](docs/evaluation/README.md)).
> For answers, a 26-question judged set and harness exist, and with real BGE-M3 retrieval
> every question's evidence reaches the model. **Answer quality with a real language model
> has not been measured yet**: no API keys or Ollama server were available, so all three
> providers are verified only with mocked HTTP transports, never against the real services
> ([docs/llm-providers.md](docs/llm-providers.md#verification-status),
> [docs/evaluation/chat.md](docs/evaluation/chat.md)).
>
> **No authentication:** anyone who can reach the server can read every document and every
> conversation. Run it on a trusted network only.

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
| Text extraction with page numbers and headings (PDFium via pypdfium2 for PDF) | ✅ Implemented |
| Japanese/English/mixed language detection and token-based multilingual chunking | ✅ Implemented |
| Local BAAI/bge-m3 dense embeddings (1024-d) stored in pgvector | ✅ Implemented (real model verified on CPU) |
| Database-backed ingestion worker (leases, retries, recovery, graceful shutdown) | ✅ Implemented |
| Document management API (upload, list, details, chunks, file, retry, reindex, delete) | ✅ Implemented |
| Documents UI (drag-and-drop upload, library, details, status polling) | ✅ Implemented |
| Frontend unit tests (Vitest + Testing Library) | ✅ Implemented |
| OCR for scanned PDFs | ❌ Not planned yet |
| Semantic search (BGE-M3 query embeddings, exact cosine similarity in pgvector) | ✅ Implemented (real model evaluated on CPU) |
| Japanese/English keyword search (Sudachi + Snowball, BM25, quoted phrases) | ✅ Implemented |
| Hybrid search with Reciprocal Rank Fusion | ✅ Implemented |
| `POST /api/v1/search` with filters, snippets, highlights, scores and method ranks | ✅ Implemented |
| Search page (modes, filters, result cards linking to the source chunk) | ✅ Implemented |
| Retrieval evaluation (51 judged EN/JA/mixed queries; Recall, Precision, MRR, nDCG, latency) | ✅ Implemented and run with the real model |
| Approximate nearest-neighbour (HNSW) index | ⏸️ Not needed yet: exact search measured and chosen ([ADR 0006](docs/decisions/0006-hybrid-retrieval.md)) |
| Reranking model | ⏸️ Not added: no measured need yet ([ADR 0006](docs/decisions/0006-hybrid-retrieval.md)) |
| LLM provider interface; Anthropic Claude via the official SDK (streaming, timeouts, retries, error mapping) | ✅ Implemented (contract-tested with a mocked transport; no real API call verified) |
| Google Gemini (official Google Gen AI SDK) and Ollama (HTTP API) providers | ✅ Implemented (contract-tested with mocked transports; no real request verified) |
| Provider registry, model catalog (`GET /chat/models`) and per-question model choice, validated on the server, with no cross-provider fallback | ✅ Implemented |
| Model selector on the Chat page (cost class, availability, reasons, remembered per browser session) | ✅ Implemented |
| Conversations API: create, list, read, rename, delete, continue | ✅ Implemented |
| Streamed answers over Server-Sent Events, with stop, retry and idempotent resend | ✅ Implemented |
| Grounded answers: numbered sources, token-budgeted context, follow-up retrieval, abstention | ✅ Implemented |
| Citations checked against retrieved chunks, stored, and relocated after reindexing | ✅ Implemented |
| Chat page (history sidebar, streaming, citations, expandable sources, document links) | ✅ Implemented |
| Answer evaluation (26 judged EN/JA/cross-language questions; lexical metrics) | ✅ Implemented; run with a non-LLM baseline only |
| Authentication | 🕒 Planned |
| AWS deployment | 🕒 Planned |

## Technology stack

| Layer | Technology |
| --- | --- |
| Backend | Python 3.12, FastAPI, Pydantic v2, pydantic-settings, Uvicorn, pypdfium2 (PDFium) |
| Embeddings (worker and API) | BAAI/bge-m3 via sentence-transformers and PyTorch (CPU, or Apple MPS on the host) |
| Keyword search | SudachiPy + sudachidict-core (Japanese), snowballstemmer (English), PostgreSQL `tsvector` + GIN, BM25 in SQL |
| Database | PostgreSQL 17, pgvector 0.8.1, SQLAlchemy 2 (async), psycopg 3, Alembic |
| Answer generation | Anthropic Claude (official `anthropic` SDK), Google Gemini (official `google-genai` SDK) and local models through Ollama's HTTP API; Claude (`claude-opus-5-5`) is the default |
| Frontend | React 19, TypeScript, Vite, Tailwind CSS v4, React Router, react-markdown |
| Tooling | uv (locked Python dependencies), Ruff (lint + format), pytest, oxlint, Vitest, Docker Compose, GitHub Actions |

## Architecture overview

```mermaid
flowchart LR
    browser([Browser]) --> fe["Frontend<br/>React + Vite"]
    fe -- "/api/*" --> be["API<br/>FastAPI"]
    be --> db[("PostgreSQL<br/>+ pgvector + tsvector")]
    cache2[("model cache<br/>volume")] --> be
    be -- "store original" --> files[("uploads<br/>volume")]
    be -- "stream answers" --> llm(["Anthropic API · Gemini API<br/>or Ollama (local)"])
    worker["Ingestion worker<br/>extract · chunk · BGE-M3"] -- "claim jobs · write chunks" --> db
    files --> worker
    cache[("model cache<br/>volume")] --> worker
    mig["Alembic<br/>migrations"] --> db
```

- The **frontend** has a Documents area (upload, library, details), a Search page, a Chat
  page and a dashboard with live backend status. Evaluation is a labelled placeholder.
- The **API** validates and stores uploads, queues ingestion jobs in PostgreSQL, serves
  document data, runs search, and answers questions: it retrieves passages, streams a
  cited answer from the language model over Server-Sent Events, and stores conversations.
  It loads BGE-M3 once to embed search queries, on a dedicated thread.
- The **worker** is a separate process that claims jobs, extracts text, chunks it, embeds it
  with BAAI/bge-m3 and writes chunks and vectors in one transaction.
- The **database** is PostgreSQL with pgvector
  ([ADR 0002](docs/decisions/0002-postgres-pgvector.md)); it is also the job queue, so
  there is no Redis or Celery.

More detail: [docs/architecture.md](docs/architecture.md) ·
[docs/ingestion-pipeline.md](docs/ingestion-pipeline.md) ·
[docs/retrieval.md](docs/retrieval.md) ·
[docs/chat.md](docs/chat.md) ·
[docs/chat-api.md](docs/chat-api.md) ·
[docs/llm-providers.md](docs/llm-providers.md) ·
[docs/llm-smoke-tests.md](docs/llm-smoke-tests.md) ·
[docs/evaluation/](docs/evaluation/README.md) ·
Decisions: [docs/decisions/](docs/decisions/)

```text
docs-rag-platform/
├── backend/             FastAPI app, ingestion worker, search, Alembic migrations, tests
│   └── evaluation/      Judged evaluation set, metrics, harness and benchmarks
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
it resolved (the default `EMBEDDING_MODEL_REVISION` pins commit `5617a9f6…`):

```bash
docker compose run --rm worker python -m app.worker prepare-model
```

The worker image is about 1.3 GB (measured on a GitHub Actions runner): on Linux, uv
installs CPU-only PyTorch from the PyTorch CPU wheel index, without the CUDA libraries the
default PyPI wheel pulls in. On macOS the PyPI wheel is used so that host-native runs can use
Apple's Metal (MPS) backend. The model weights are never part of the image. The worker
container is limited to 4 CPUs and 6 GB of memory by default (`WORKER_CPUS`,
`WORKER_MEMORY`); BGE-M3 on CPU needs roughly 4 to 6 GB.

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

### Choosing language models

Chat needs at least one provider. Configure any combination in `.env`, then restart the API;
the Chat page's model selector lists each model with its cost class and whether it can be
used. Details, limits and data handling: [docs/llm-providers.md](docs/llm-providers.md).

| Provider | Cost | Set | Notes |
| --- | --- | --- | --- |
| Anthropic Claude | Paid per token | `ANTHROPIC_API_KEY` | The Phase 4 default. Key from the Anthropic Console |
| Google Gemini | Free tier or paid | `GEMINI_API_KEY` (and `GEMINI_TIER`) | Key from [Google AI Studio](https://aistudio.google.com/app/apikey). **On the free tier Google may use prompts and document passages to improve its products, and rate limits are low**; do not use it for confidential documents ([details](docs/llm-providers.md#free-tier-limits-and-data-handling)) |
| Ollama | Local, free | `OLLAMA_MODELS` (and `OLLAMA_BASE_URL` outside Docker) | Install from <https://ollama.com/download>, then `ollama pull qwen3:8b`. Prompts stay on your machine; answers are slower ([macOS setup](docs/llm-providers.md#installing-ollama-on-macos)) |

For example, a setup that costs nothing per question, with the stack in Docker Desktop on a
Mac and Ollama running as the Mac app:

```bash
# .env
LLM_PROVIDER=ollama
OLLAMA_MODELS=qwen3:8b          # after `ollama pull qwen3:8b`
# OLLAMA_BASE_URL defaults to http://host.docker.internal:11434 in Compose;
# use http://localhost:11434 when the API runs directly on the Mac (Option B).
```

`LLM_PROVIDER` / `LLM_MODEL` only choose the default. The server accepts only models it
lists, and it never switches to another provider or model when the chosen one fails.

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
| `EMBEDDING_MODEL_REVISION` | no | Commit SHA, branch or tag. Default `5617a9f61b028005a4858fdac845db406aefb181`, the BAAI/bge-m3 commit verified for Phase 2. Changing it means reindexing for consistent vectors. |
| `EMBEDDING_DIMENSION` | no | Must be `1024` (the `vector(1024)` column). |
| `EMBEDDING_DEVICE` | no | `auto` (MPS if available, else CPU), `cpu`, `mps` or `cuda`. Default `auto`; Compose uses `cpu`. On Linux the locked PyTorch is CPU-only, so `cuda` needs a different PyTorch build. |
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
| `SEARCH_DEFAULT_MODE` | no | Mode for requests that name none: `semantic` (default), `keyword` or `hybrid`. |
| `SEARCH_DEFAULT_TOP_K` / `SEARCH_MAX_TOP_K` | no | Default and maximum number of results. Defaults `10` / `50`. |
| `SEARCH_MAX_QUERY_CHARS` | no | Longest accepted query. Default `1000`. |
| `SEARCH_FUSION_DEPTH` / `SEARCH_RRF_K` | no | Candidates per method for hybrid search, and the RRF constant. Defaults `50` / `60`. |
| `SEARCH_BM25_K1` / `SEARCH_BM25_B` | no | BM25 parameters. Defaults `1.2` / `0.75`. |
| `SEARCH_SNIPPET_CHARS` | no | Snippet length. Default `280`. |
| `SEARCH_PRELOAD_MODEL` | no | Load the model when the API starts. Default `false`; Compose uses `true`. |
| `SEARCH_MODEL_LOAD_WAIT_SECONDS` | no | How long a search waits for a model load in progress. Default `20`. |
| `BACKEND_MEMORY` / `BACKEND_TORCH_THREADS` | Compose only | API container memory limit and PyTorch threads. Defaults `4g` / `2`. |
| `LLM_PROVIDER` / `LLM_MODEL` | no | Default model for questions that name none: `anthropic` and, when `LLM_MODEL` is unset, the first model of that provider's list. |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODELS` | for Claude | Server-side key (never commit it) and the Claude models offered. Without any provider configured the API starts and chat answers `503 llm_not_configured`. |
| `GEMINI_API_KEY` / `GEMINI_MODELS` / `GEMINI_TIER` / `GEMINI_THINKING_LEVEL` | for Gemini | Server-side key, models offered (Flash and Flash-Lite by default), `free` or `paid`, thinking level (unset). |
| `OLLAMA_BASE_URL` / `OLLAMA_MODELS` | for Ollama | Ollama server (Compose default `http://host.docker.internal:11434`) and the allowlist of local models; Ollama is off while `OLLAMA_MODELS` is empty. |
| `OLLAMA_NUM_CTX` / `OLLAMA_THINK` / `OLLAMA_KEEP_ALIVE` / `OLLAMA_ALLOW_REMOTE_MODELS` | no | Context window (`16384`), think option, keep-alive (unset), allow Ollama cloud models (`false`). |
| `LLM_CATALOG_TTL_SECONDS` / `LLM_CATALOG_CHECK_TIMEOUT_SECONDS` | no | How long a model availability check is reused (`300`) and its timeout (`5`). |
| `LLM_MAX_OUTPUT_TOKENS` / `LLM_EFFORT` / `LLM_TEMPERATURE` / `LLM_FALLBACKS` | no | Output limit (`2048`), Claude effort (unset), temperature (unset; current Claude models reject it), Claude server-side fallback (`default`). |
| `LLM_CONNECT_TIMEOUT_SECONDS` / `LLM_READ_TIMEOUT_SECONDS` / `LLM_REQUEST_TIMEOUT_SECONDS` / `LLM_MAX_RETRIES` | no | `10` / `60` / `180` / `2`. See [docs/llm-providers.md](docs/llm-providers.md). |
| `CHAT_RETRIEVAL_MODE` / `CHAT_RETRIEVAL_TOP_K` / `CHAT_MIN_SIMILARITY` | no | Retrieval for answers: mode (default `SEARCH_DEFAULT_MODE`), passages (`8`), similarity floor (unset). |
| `CHAT_CONTEXT_TOKEN_BUDGET` / `CHAT_HISTORY_TOKEN_BUDGET` / `CHAT_HISTORY_MAX_MESSAGES` | no | Prompt budgets: `8000` / `3000` estimated tokens, `10` messages. |
| `CHAT_MAX_QUESTION_CHARS` / `CHAT_MAX_CONCURRENT_GENERATIONS` | no | `4000` characters; `4` answers at once per API process. |
| `CHAT_FOLLOWUP_MAX_CHARS` / `CHAT_CITATION_EXCERPT_CHARS` / `CHAT_HEARTBEAT_SECONDS` / `CHAT_MAX_TITLE_CHARS` | no | `60` / `600` / `15` / `200`. See [docs/chat.md](docs/chat.md). |

## Testing

Backend dependencies are pinned in [`backend/uv.lock`](backend/uv.lock). From `backend/`:

```bash
uv sync                         # install the locked versions
uv run ruff check .             # lint
uv run ruff format --check .    # formatting
uv run pytest                   # unit tests; integration tests are skipped without a database
```

To also run the integration tests (readiness, migrations, the document API, the job queue
and the worker against a real database), point `TEST_DATABASE_URL` at a PostgreSQL +
pgvector server that has been migrated. The test user needs permission to create databases, because some tests create
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

#### Real language model smoke tests

`tests/test_llm_live_smoke.py` sends a few short real requests to each configured provider
(availability, one streamed answer with usage, stopping a stream). It is skipped unless
`RUN_LLM_SMOKE_TESTS=1`, never runs in CI, and costs money for paid providers. See
[docs/llm-smoke-tests.md](docs/llm-smoke-tests.md):

```bash
RUN_LLM_SMOKE_TESTS=1 GEMINI_API_KEY=… uv run pytest -m live_llm -s
```

#### Retrieval evaluation

`backend/evaluation/` holds a judged evaluation set (13 documents, 51 English, Japanese and
mixed queries) and a harness that indexes it through the worker pipeline and reports
Recall@k, Precision@k, MRR@k, nDCG@k and latency per mode, language and query type. It needs
a migrated database whose name ends in `_eval` or `_test` (`--reset` deletes its documents):

```bash
createdb yashrag_eval   # or CREATE DATABASE yashrag_eval in psql
DATABASE_URL=postgresql+psycopg://…/yashrag_eval uv run alembic upgrade head
# All modes with the real model (needs the embeddings extra and the model download):
DATABASE_URL=postgresql+psycopg://…/yashrag_eval uv run --extra embeddings python -m evaluation.run --reset
# Keyword mode only, without the model:
DATABASE_URL=postgresql+psycopg://…/yashrag_eval ENVIRONMENT=test EMBEDDING_PROVIDER=fake \
  uv run python -m evaluation.run --reset --modes keyword
```

#### Answer evaluation

`python -m evaluation.chat_eval` runs 26 judged questions through the real chat pipeline
and reports evidence recall, fact recall, citation precision and coverage, abstention,
answer language, latency and tokens. Without an API key it uses an extractive baseline (no
language model) and says so in its report; `--provider configured` uses the configured
model. See [docs/evaluation/chat.md](docs/evaluation/chat.md).

```bash
DATABASE_URL=postgresql+psycopg://…/yashrag_eval ENVIRONMENT=test EMBEDDING_PROVIDER=fake \
  uv run python -m evaluation.chat_eval --reset --provider extractive --mode keyword
```

Tests never call a real language model: they use a scripted test double and contract tests
that drive the real `anthropic` SDK against a mocked HTTP transport.

Benchmarks: `python -m evaluation.ann_benchmark` (exact vs HNSW vector search) and
`python -m evaluation.keyword_benchmark` (keyword latency vs corpus size). Results and how
to read them: [docs/evaluation/](docs/evaluation/README.md).

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

- **Real-model answers are unverified.** No Anthropic or Gemini API key and no Ollama
  server were available, so no answer from a real model has been generated or evaluated,
  and answer quality will differ between models; see
  [docs/evaluation/chat.md](docs/evaluation/chat.md). Answers can be wrong even when cited:
  the server checks that every citation points at a retrieved passage, not that the passage
  supports the sentence.
- **Chat has no per-client rate limit** (there are no users to tell apart) and every
  question to a paid model costs tokens. Anyone who can reach the server can use any
  configured model, including paid ones.
- **Gemini's free tier** has low rate limits and lets Google use submitted content, which
  includes passages from your documents
  ([details](docs/llm-providers.md#free-tier-limits-and-data-handling)). Limits are per conversation (one answer at a time) and per
  API process (`CHAT_MAX_CONCURRENT_GENERATIONS`). See
  [docs/chat.md](docs/chat.md#security-and-limitations).
- **Semantic search is exact.** Latency grows linearly with the number of chunks (about
  50 ms at 10,000 and 620 to 670 ms at 100,000 on 4 vCPUs); an HNSW index is the planned
  next step past roughly 17,000 chunks ([ADR 0006](docs/decisions/0006-hybrid-retrieval.md)).
- **Semantic and hybrid search always return results**, even when no document answers the
  query, unless `min_similarity` is set. Keyword search does not match across languages.
- **The evaluation set is small** (51 queries, written for this project), with few
  identifier-style queries. Treat its numbers as a regression baseline, not a benchmark.
- More details: [docs/retrieval.md](docs/retrieval.md#known-limitations).
- Real BGE-M3 has been run on CPU only (GitHub Actions); MPS on Apple Silicon and the
  Docker containers with the real model have not been tested. The API and the worker each
  hold a copy of the model in memory (about 2.3 GB each).
- No OCR: scanned PDFs fail with `no_extractable_text`. Only PDF, Markdown and plain text.
- Dense vectors only; no sparse or multi-vector (ColBERT) retrieval.
- No authentication or authorization, and therefore no per-user documents or conversations:
  anyone who can reach the API can upload, read and delete every document, and read,
  rename and delete every conversation. Conversations are not private between people, and
  deduplication is global.
  Do not expose the stack to the public internet.
- Original files are stored on local disk (a Docker volume) with no backups. The storage
  interface is ready for object storage, but no S3 implementation exists.
- PDF parsing uses [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) (Google's
  PDFium; BSD-3-Clause / Apache-2.0), so the backend has no AGPL dependency. PyMuPDF
  (AGPL-3.0) was used during early Phase 2 development and has been removed. The one
  remaining weak-copyleft dependency is psycopg (LGPL-3.0), used unmodified as a library.
  Paragraphs are rebuilt from line spacing and font size, so unusual layouts (multi-column
  pages, tables) may be split differently from how they look.
- The Docker setup is for local development only (Vite dev server, `uvicorn --reload`,
  source mounts). The API and worker images are about 1.5 GB each (measured on GitHub
  Actions) because they contain PyTorch (CPU-only) and the Sudachi dictionary; they share
  their dependency layers. Building them needs access to `download.pytorch.org`.
- The UI is in English only.

## Roadmap

| Phase | Scope | Status |
| --- | --- | --- |
| 1. Foundation | Monorepo, FastAPI, PostgreSQL + pgvector, migrations, app shell, Docker, CI | ✅ Done |
| 2. Ingestion | Document upload, JA/EN parsing and chunking, local BGE-M3 embeddings, worker, Documents UI | ✅ Done |
| 3. Retrieval | Semantic, bilingual keyword and hybrid search, search API and UI, retrieval evaluation | ✅ Done |
| 4. Chat | Grounded, streamed answers with citations, conversations, chat UI, answer evaluation harness | ✅ Implemented (real-model run pending an API key) |
| 4.1 Multi-model | Gemini and Ollama providers, model catalog, per-question model selector | ✅ Implemented (real-provider runs pending keys and an Ollama server) |
| 5. Evaluation | Evaluation UI, model-based or human answer review (lexical answer metrics exist since Phase 4) | 🕒 Planned |
| 6. Deployment | Production images, AWS infrastructure, monitoring | 🕒 Planned |

## License

Released under the [MIT License](LICENSE).
