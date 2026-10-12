# Infrastructure

Deployment configuration for YashRAG lives here.

**Current status (Phase 2):** nothing is deployed. The only runtime configuration is the
local development stack in [`docker-compose.yml`](../docker-compose.yml) at the repository
root, which runs PostgreSQL + pgvector, the FastAPI backend, the ingestion worker and the
Vite dev server, with named volumes for the database, uploaded files and the model cache.

**Planned:** AWS deployment configuration (for example infrastructure-as-code for the
container service, a managed PostgreSQL instance with pgvector, and static frontend hosting)
will be added here in a later phase. The target design will be recorded as an architecture
decision record in [`docs/decisions/`](../docs/decisions/) before it is built.
