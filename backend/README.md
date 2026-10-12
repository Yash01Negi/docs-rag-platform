# YashRAG backend

FastAPI service for YashRAG. See the [repository README](../README.md) for setup and the
[architecture notes](../docs/architecture.md) for how the pieces fit together.

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp ../.env.example ../.env      # then edit DATABASE_URL if needed
alembic upgrade head
uvicorn app.main:create_app --factory --reload
```
