# YashRAG

**A bilingual (Japanese/English) document intelligence platform built on
retrieval-augmented generation (RAG).**

YashRAG is being built in phases. The goal is a system where you can upload Japanese and
English documents, ask questions in either language, and get answers grounded in those
documents with citations, backed by an evaluation suite that measures retrieval and answer
quality.

> **Status: Phase 1 (Foundation) is complete.** The platform skeleton runs end to end:
> API, database with pgvector, frontend shell, Docker Compose and CI. **No RAG features
> are implemented yet.** Document ingestion, retrieval, chat and evaluation are planned for
> later phases and are clearly marked as such in the UI.

## Implementation status

| Area | Status |
| --- | --- |
| FastAPI backend with `/api/v1/health` (liveness) and `/api/v1/ready` (database readiness) | ✅ Implemented |
| Environment-based configuration (pydantic-settings), no default credentials | ✅ Implemented |
| Structured JSON logging with per-request IDs | ✅ Implemented |
| Consistent JSON error responses | ✅ Implemented |
| CORS with an explicit origin allowlist | ✅ Implemented |
| PostgreSQL 17 + pgvector, Alembic migrations (extension only, no tables yet) | ✅ Implemented |
| React + TypeScript + Tailwind application shell with live backend status | ✅ Implemented |
| Docker Compose development stack with health checks | ✅ Implemented |
| GitHub Actions CI (lint, tests, type check, build, compose validation) | ✅ Implemented |
| Document upload and ingestion (Japanese/English parsing and chunking) | 🕒 Planned |
| Embeddings stored in pgvector | 🕒 Planned |
| Hybrid retrieval (vector + keyword) and reranking | 🕒 Planned |
| Chat with grounded answers and citations | 🕒 Planned |
| Retrieval and answer-quality evaluation | 🕒 Planned |
| Authentication | 🕒 Planned |
| AWS deployment | 🕒 Planned |

## Technology stack

| Layer | Technology |
| --- | --- |
| Backend | Python 3.12, FastAPI, Pydantic v2, pydantic-settings, Uvicorn |
| Database | PostgreSQL 17, pgvector 0.8.1, SQLAlchemy 2 (async), psycopg 3, Alembic |
| Frontend | React 19, TypeScript, Vite, Tailwind CSS v4, React Router |
| Tooling | Ruff (lint + format), pytest, Docker Compose, GitHub Actions |

## Architecture overview

```mermaid
flowchart LR
    browser([Browser]) --> fe["Frontend<br/>React + Vite"]
    fe -- "/api/*" --> be["Backend<br/>FastAPI"]
    be --> db[("PostgreSQL<br/>+ pgvector")]
    mig["Alembic<br/>migrations"] --> db
```

- The **frontend** is an application shell (sidebar, header, pages) that polls the backend's
  health and readiness endpoints and shows the result.
- The **backend** exposes versioned endpoints under `/api/v1`. `/health` reports that the
  process is alive; `/ready` checks that the database answers queries and that the pgvector
  extension is installed.
- The **database** is PostgreSQL with pgvector, chosen so that relational data, embeddings
  and keyword search can live in one system
  ([ADR 0002](docs/decisions/0002-postgres-pgvector.md)).

More detail: [docs/architecture.md](docs/architecture.md) ·
Decisions: [docs/decisions/](docs/decisions/)

```text
docs-rag-platform/
├── backend/             FastAPI app, Alembic migrations, tests (pyproject.toml)
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
the backend and the frontend once their dependencies are healthy.

| Service | URL |
| --- | --- |
| Frontend | <http://localhost:5173> |
| API liveness | <http://localhost:8000/api/v1/health> |
| API readiness | <http://localhost:8000/api/v1/ready> |
| OpenAPI docs | <http://localhost:8000/api/v1/docs> |

Stop with `docker compose down`. Add `-v` to also delete the database volume.

### Option B: Run the backend and frontend on your machine

Requirements: Python 3.12, Node.js 22+, and a PostgreSQL server with pgvector. The simplest
way to get the database is to start only that service from Compose:

```bash
cp .env.example .env
docker compose up -d db
```

Backend (the root `.env` is read automatically):

```bash
cd backend
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
alembic upgrade head
uvicorn app.main:create_app --factory --reload
```

Frontend (in another terminal):

```bash
cd frontend
npm ci
npm run dev                     # http://localhost:5173, proxies /api to localhost:8000
```

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

## Testing

Backend (from `backend/`, with the virtual environment active):

```bash
ruff check .                    # lint
ruff format --check .           # formatting
pytest                          # unit tests; integration tests are skipped without a database
```

To also run the integration tests (readiness against a real database, migration
upgrade/downgrade), point `TEST_DATABASE_URL` at a PostgreSQL + pgvector server that has
been migrated. The test user needs permission to create databases, because some tests create
and drop a temporary database.

```bash
export TEST_DATABASE_URL="$DATABASE_URL"
alembic upgrade head
pytest
```

Frontend (from `frontend/`):

```bash
npm run typecheck               # TypeScript (tsc -b)
npm run build                   # production build into dist/
```

CI runs all of the above on every push to `main` and every pull request.

## Current limitations

- No RAG functionality yet: no document upload, embeddings, retrieval, chat or evaluation.
- No authentication or authorization. Do not expose the stack to the public internet.
- The Docker setup is for local development only (Vite dev server, `uvicorn --reload`,
  source mounts). Production images and deployment are not built yet.
- No database tables beyond the `vector` extension.
- No frontend automated tests yet; the frontend is checked by TypeScript and the build.
- The UI is in English only; Japanese UI localization is planned alongside the bilingual
  document features.

## Roadmap

| Phase | Scope | Status |
| --- | --- | --- |
| 1. Foundation | Monorepo, FastAPI, PostgreSQL + pgvector, migrations, app shell, Docker, CI | ✅ Done |
| 2. Ingestion | Document upload, JA/EN parsing and chunking, embeddings, document list UI | 🕒 Next |
| 3. Retrieval | Vector + keyword hybrid search, reranking, retrieval API | 🕒 Planned |
| 4. Chat | Grounded answer generation with citations, chat UI | 🕒 Planned |
| 5. Evaluation | Bilingual evaluation set, retrieval and answer-quality metrics, evaluation UI | 🕒 Planned |
| 6. Deployment | Production images, AWS infrastructure, monitoring | 🕒 Planned |

## License

Released under the [MIT License](LICENSE).
