# YashRAG backend

FastAPI service for YashRAG. See the [repository README](../README.md) for setup and the
[architecture notes](../docs/architecture.md) for how the pieces fit together.

```bash
uv sync                         # install locked dependencies (uv.lock) into .venv
cp ../.env.example ../.env      # then edit DATABASE_URL if needed
uv run alembic upgrade head
uv run uvicorn app.main:create_app --factory --reload
```
