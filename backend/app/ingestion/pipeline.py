"""Turn a stored file into chunks and embeddings (CPU-bound; runs in a worker thread)."""

import logging
import time
from pathlib import Path

from app.db.models import FileType
from app.embeddings.base import EmbeddingProvider
from app.ingestion.chunking import ChunkingConfig, chunk_blocks
from app.ingestion.errors import ErrorCode, IngestionError
from app.ingestion.extraction import extract
from app.ingestion.jobs import IndexResult
from app.ingestion.language import detect_language

logger = logging.getLogger(__name__)


def build_index(
    path: Path,
    file_type: FileType,
    provider: EmbeddingProvider,
    chunking: ChunkingConfig,
) -> IndexResult:
    started = time.perf_counter()
    model = provider.load()
    document = extract(path, file_type)
    chunks = chunk_blocks(document.blocks, provider.count_tokens, chunking)
    if not chunks:
        raise IngestionError(ErrorCode.NO_EXTRACTABLE_TEXT, "No text could be chunked.")
    extracted_at = time.perf_counter()
    vectors = provider.embed([chunk.text for chunk in chunks])
    logger.info(
        "index_built",
        extra={
            "chunks": len(chunks),
            "tokens": sum(chunk.token_count for chunk in chunks),
            "pages": document.page_count,
            "extract_chunk_ms": round((extracted_at - started) * 1000),
            "embed_ms": round((time.perf_counter() - extracted_at) * 1000),
        },
    )
    return IndexResult(
        chunks=chunks,
        vectors=vectors,
        language=detect_language(document.text).value,
        page_count=document.page_count,
        model=model,
    )
