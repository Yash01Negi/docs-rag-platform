# ADR 0003: Run BAAI/bge-m3 locally for dense embeddings

- **Status:** Accepted
- **Date:** 2026-10-12

## Context

Phase 2 stores a dense embedding for every chunk so that later phases can do vector
retrieval. Documents are Japanese, English, or a mix of both, and questions may be asked in
either language about a document in the other one. The embedding model therefore has to:

- place Japanese and English text with the same meaning close together (cross-lingual);
- handle Japanese without relying on whitespace word boundaries;
- run without sending document text to a third-party API, so that private documents can be
  indexed and the project can be run by anyone without an API key;
- produce a fixed-size vector that fits a pgvector column.

Options considered:

1. **A hosted embedding API** (for example OpenAI, Cohere or Voyage). Strong models and no
   local hardware cost, but every chunk of every document leaves the machine, it needs a
   paid key, and the index depends on a model version the project does not control.
2. **multilingual-e5 (base/large)**. Good multilingual quality and smaller than BGE-M3, but a
   512-token input limit and a required `query:` / `passage:` prefix convention.
3. **BAAI/bge-m3**. A multilingual XLM-RoBERTa-large model (about 568M parameters) with
   strong published cross-lingual results, Japanese included, and an 8192-token input
   limit. It also offers sparse and multi-vector (ColBERT) outputs.

## Decision

Use **BAAI/bge-m3 dense embeddings, 1024 dimensions, run locally** by the ingestion worker.

- The model is built explicitly as *Transformer → CLS pooling → L2 normalization*. This is
  how the upstream FlagEmbedding library produces BGE-M3 dense vectors
  (`pooling_method="cls"`, normalized), and building the pipeline in code means the result
  does not depend on whichever pooling configuration happens to ship with the weights.
- Only the dense output is used. Sparse and multi-vector retrieval are out of scope; the
  weight download skips the ColBERT and sparse heads and the ONNX export.
- The model runs through `sentence-transformers` and PyTorch, which are an **optional
  dependency extra** (`embeddings`). The API process and CI never install them.
- The provider sits behind an `EmbeddingProvider` interface (`app/embeddings/base.py`) with
  a deterministic fake implementation for tests. The fake is refused at startup unless
  `ENVIRONMENT=test`, so it cannot silently produce a production index.
- Every vector is checked before it is stored: exact count, 1024 dimensions, finite values,
  unit norm (within 1e-3). Any failure fails the job. Random or placeholder vectors are never
  substituted.
- Inputs longer than `EMBEDDING_MAX_SEQ_LENGTH` (default 1024 tokens) are rejected, never
  truncated. The chunker counts tokens with the model's own tokenizer, and settings
  validation guarantees the largest possible chunk fits.
- Weights are downloaded at runtime into a persistent cache (`MODEL_CACHE_DIR`, a named
  volume in Compose), never during `docker build`. `EMBEDDING_MODEL_REVISION` accepts a
  commit SHA; `python -m app.worker prepare-model` downloads the weights and prints the SHA
  it resolved so it can be pinned. The resolved commit is stored on every indexed
  document (`documents.embedding_model_revision`).
- Device selection: `EMBEDDING_DEVICE=auto` uses Apple's Metal (MPS) backend when PyTorch
  reports it, otherwise CPU. On Linux, PyTorch comes from PyTorch's CPU-only wheel index
  (an explicit uv index in `backend/pyproject.toml`), so `cuda` is not available with the
  locked dependencies; macOS uses the PyPI wheel, which supports MPS.

## Licensing

- The **FlagEmbedding** code that defines BGE-M3's dense pooling is MIT-licensed (checked
  in the FlagEmbedding repository).
- The **BAAI/bge-m3 weights** are published on Hugging Face under the MIT license according
  to the upstream project. The model card could not be fetched from this project's build
  environment (the Hugging Face Hub was blocked by the network policy), so check the license
  on the [model card](https://huggingface.co/BAAI/bge-m3) before redistributing the weights.
- The weights are not stored in this repository and not baked into any image.
- `sentence-transformers` (Apache-2.0) and PyTorch (BSD-3-Clause) are runtime dependencies
  of the worker only.

## Consequences

**Benefits**

- No document text leaves the machine during ingestion, and no API key is needed.
- One model covers Japanese, English and mixed documents, with a long input limit, so chunk
  size is a retrieval decision rather than a model constraint.
- Indexes are reproducible when the revision is pinned, and every document records which
  model and revision produced its vectors.

**Costs and risks**

- **Size.** The fp32 weights are about 2.3 GB. Plan for 4 to 6 GB of RAM for the worker on
  CPU; the Compose default limit is 6 GB.
- **Speed.** CPU inference for a model of this size is slow for large documents. On Apple
  Silicon, running the worker on the host (not in Docker) lets it use MPS; Docker Desktop on
  macOS cannot reach the Apple GPU. No throughput numbers are claimed here; none were
  measured.
- **Image size.** The default PyTorch wheels on PyPI for Linux (x86_64 and aarch64) depend
  on about 4 GB of CUDA, cuDNN, NCCL and Triton packages, which made the first worker image
  5.8 GB on disk. Linux now installs `torch==2.14.1+cpu` from the PyTorch CPU index, and the
  worker image measured 1.32 GB (`docker image inspect`, GitHub Actions runner), with
  `import torch` and a matrix multiply checked inside it. The cost: building the worker
  image needs `download.pytorch.org` as well as PyPI, and GPU inference on Linux would need
  a different PyTorch build.
- **Verified on CPU only.** The model could not be downloaded where Phase 2 was built
  (Hugging Face returned HTTP 403 through the network proxy), so it was run on a GitHub
  Actions Linux runner with the locked CPU-only PyTorch. `prepare-model` resolved revision
  `5617a9f61b028005a4858fdac845db406aefb181`; the opt-in smoke test (`RUN_MODEL_TESTS=1`)
  passed (4 tests); and indexing a generated English and Japanese PDF produced
  1024-dimension, unit-norm vectors, with a cosine similarity of 0.85 between the first
  English and first Japanese chunk (the same text in both languages). MPS on Apple Silicon
  and the Docker worker image have not been run with the real model.
- **Changing the model means reindexing.** The `chunks.embedding` column is `vector(1024)`.
  A model with another dimension needs a migration and a reindex of every document;
  settings validation refuses any other dimension.
