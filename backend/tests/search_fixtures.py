"""Helpers that put indexed documents straight into the test database for search tests.

Chunks are stored exactly as the worker stores them (fake-provider vectors and analyzer
search vectors), without running extraction, so each test controls its chunk texts.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import create_engine, text

from app.core.config import Settings
from app.embeddings.fake import FAKE_MODEL_NAME, FAKE_MODEL_REVISION, FakeEmbeddingProvider
from app.ingestion.language import detect_language
from app.search.lexical_index import lexical_entry

_FAKE = FakeEmbeddingProvider(1024)


@dataclass(frozen=True)
class SeedChunk:
    text: str
    page: int | None = None
    section: str | None = None


@dataclass(frozen=True)
class SeededDocument:
    id: uuid.UUID
    chunk_ids: list[uuid.UUID]


def fake_vector(text_: str) -> list[float]:
    return _FAKE.embed([text_])[0]


def seed_document(
    settings: Settings,
    filename: str,
    chunks: Sequence[SeedChunk | str],
    *,
    revision: str = FAKE_MODEL_REVISION,
    status: str = "completed",
) -> SeededDocument:
    items = [chunk if isinstance(chunk, SeedChunk) else SeedChunk(chunk) for chunk in chunks]
    document_id = uuid.uuid4()
    chunk_ids = [uuid.uuid4() for _ in items]
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO documents (id, filename, file_type, mime_type, size_bytes, "
                    "sha256, storage_key, language, page_count, status, chunk_count, "
                    "embedding_model, embedding_model_revision, embedding_dimension, "
                    "processed_at) VALUES (:id, :filename, 'text', 'text/plain', 100, :sha, "
                    ":key, :language, NULL, :status, :count, :model, :revision, 1024, now())"
                ),
                {
                    "id": document_id,
                    "filename": filename,
                    "sha": uuid.uuid4().hex * 2,
                    "key": uuid.uuid4().hex,
                    "language": detect_language(" ".join(i.text for i in items)).value,
                    "status": status,
                    "count": len(items),
                    "model": FAKE_MODEL_NAME,
                    "revision": revision,
                },
            )
            for ordinal, (chunk_id, item) in enumerate(zip(chunk_ids, items, strict=True)):
                entry = lexical_entry(item.text)
                connection.execute(
                    text(
                        "INSERT INTO chunks (id, document_id, ordinal, text, token_count, "
                        "language, page_start, page_end, section, embedding, search_vector, "
                        "search_length) VALUES (:id, :document_id, :ordinal, :text, 10, "
                        ":language, :page, :page, :section, CAST(:embedding AS vector), "
                        "CAST(:vector AS tsvector), :length)"
                    ),
                    {
                        "id": chunk_id,
                        "document_id": document_id,
                        "ordinal": ordinal,
                        "text": item.text,
                        "language": detect_language(item.text).value,
                        "page": item.page,
                        "section": item.section,
                        "embedding": str(fake_vector(item.text)),
                        "vector": entry.vector,
                        "length": entry.length,
                    },
                )
    finally:
        engine.dispose()
    return SeededDocument(document_id, chunk_ids)
