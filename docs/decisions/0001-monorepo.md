# ADR 0001: Use a single monorepo for backend, frontend and infrastructure

- **Status:** Accepted
- **Date:** 2026-10-12

## Context

YashRAG has a Python API, a React frontend, database migrations, deployment configuration
and documentation. During early development these parts change together: a new API endpoint
usually needs a frontend change, a migration and a documentation update.

The project is developed by one person and is also meant to be read by others (reviewers,
recruiters, future contributors), so it should be easy to clone, start and understand.

## Decision

Keep everything in one repository with a top-level directory per concern:

| Directory | Contents |
| --- | --- |
| `backend/` | FastAPI application, Alembic migrations, pytest suite (`pyproject.toml`) |
| `frontend/` | React + TypeScript + Vite application (`package.json`) |
| `docs/` | Architecture notes and architecture decision records (ADRs) |
| `infra/` | Deployment configuration (empty until a deployment phase) |
| `.github/workflows/` | CI for all of the above |

Each application keeps its own native tooling (pip/`pyproject.toml` for Python, npm for the
frontend). There is no monorepo build tool such as Nx, Turborepo or Bazel.

## Consequences

- One clone and one `docker compose up` starts the whole system.
- A cross-cutting change (API + UI + migration + docs) lands in a single commit or pull
  request and is reviewed together.
- CI runs backend and frontend jobs in parallel from one workflow file.
- Without a monorepo build tool, CI does not skip unaffected jobs. At the current size this
  costs a minute or two per run, which is acceptable. Path filters can be added later if CI
  time becomes a problem.
- If the frontend and backend ever need independent release cycles or ownership, they can
  be split into separate repositories; the directory boundaries already make that a
  mechanical change.
