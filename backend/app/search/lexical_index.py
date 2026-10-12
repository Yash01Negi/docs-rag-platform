"""Building and rebuilding the stored keyword-search vectors of chunks.

New chunks get their vector from the worker in the same transaction that stores them
(:func:`app.ingestion.jobs.complete_job`). This module (re)computes the vectors of chunks that
already exist: migration 0003 uses it to backfill, and
``python -m app.worker rebuild-search-index`` uses it after the analyzer changes.
"""

import logging
from dataclasses import dataclass

from sqlalchemy import Connection, text

from app.search.analyzer import analyze, to_tsvector_literal

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class LexicalEntry:
    """What is stored for one chunk: the ``tsvector`` literal and the BM25 length."""

    vector: str
    length: int


def lexical_entry(chunk_text: str) -> LexicalEntry:
    tokens = analyze(chunk_text)
    return LexicalEntry(vector=to_tsvector_literal(tokens), length=len(tokens))


def rebuild_lexical_index(connection: Connection, batch_size: int = 500) -> int:
    """Recompute ``search_vector`` and ``search_length`` for every chunk; returns the count.

    Runs in batches ordered by chunk ID. Each chunk is updated on its own, so concurrent
    ingestion is safe: a chunk deleted meanwhile is simply skipped, and new chunks already
    have current vectors.
    """
    updated = 0
    last_id = None
    while True:
        rows = connection.execute(
            text(
                "SELECT id, text FROM chunks WHERE (CAST(:last AS uuid) IS NULL OR id > :last) "
                "ORDER BY id LIMIT :limit"
            ),
            {"last": last_id, "limit": batch_size},
        ).all()
        if not rows:
            break
        entries = [(row.id, lexical_entry(row.text)) for row in rows]
        connection.execute(
            text(
                "UPDATE chunks SET search_vector = CAST(:vector AS tsvector), "
                "search_length = :length WHERE id = :id"
            ),
            [{"id": cid, "vector": e.vector, "length": e.length} for cid, e in entries],
        )
        updated += len(rows)
        last_id = rows[-1].id
        logger.info("search_index_rebuild_progress", extra={"chunks": updated})
    return updated
