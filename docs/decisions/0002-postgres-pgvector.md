# ADR 0002: Use PostgreSQL with pgvector for relational data and vector search

- **Status:** Accepted
- **Date:** 2026-10-12

## Context

Later phases need to store:

- relational data: documents, chunks, ingestion jobs, chat sessions, evaluation runs;
- dense embeddings for semantic (vector) search;
- text suitable for keyword search, so retrieval can be **hybrid** (vector + keyword).

Options considered:

1. **PostgreSQL + pgvector** – one database for relational data, vectors and full-text search.
2. **PostgreSQL + a dedicated vector database** (for example Qdrant, Weaviate, Milvus or
   Pinecone) – best-in-class vector features, but two systems to run and keep consistent.
3. **A search engine** (OpenSearch/Elasticsearch) holding both keyword and vector indexes,
   plus PostgreSQL for application data.

## Decision

Use **PostgreSQL with the pgvector extension** (Docker image `pgvector/pgvector:0.8.1-pg17`).
The extension is enabled by the first Alembic migration (`0001_enable_pgvector`), so every
environment gets it through the normal migration path rather than through manual setup.

## Consequences

**Benefits**

- One stateful service to run locally, in CI and in production. Managed options exist on
  AWS (Amazon RDS and Aurora PostgreSQL support pgvector).
- Documents, chunks and their embeddings can be written in one transaction, so the vector
  index never points at a chunk that does not exist.
- Metadata filtering (by document, language, upload date, …) is ordinary SQL in the same
  query as the vector search.
- PostgreSQL full-text search can supply the keyword side of hybrid retrieval.
- Schema changes go through Alembic like any other table.

**Costs and risks**

- Very large corpora (tens of millions of vectors) or very high query rates are better
  served by dedicated vector databases. This project does not expect that scale; if it
  does, retrieval will be behind an interface so the store can be replaced.
- PostgreSQL's built-in full-text search has no Japanese tokenizer. Japanese keyword search
  will need either application-side tokenization (for example a morphological analyzer)
  before indexing, or an extension such as PGroonga. This will be decided, and recorded in
  its own ADR, when keyword retrieval is built.
- Approximate index choice (HNSW vs IVFFlat) and its parameters affect recall and latency.
  They will be chosen with measurements from the evaluation phase, not up front.
