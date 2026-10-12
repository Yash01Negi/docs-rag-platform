"""Reading conversations and checking whether cited passages still exist."""

import uuid
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models import Conversation, Message, MessageCitation


class SourceStatus(StrEnum):
    """Where a cited passage is now."""

    AVAILABLE = "available"  # the exact chunk still exists
    RELOCATED = "relocated"  # the document was reindexed; the same text is in a new chunk
    CHANGED = "changed"  # the document still exists but the passage text is gone
    DELETED = "deleted"  # the document was deleted


@dataclass(frozen=True)
class SourceLocation:
    status: SourceStatus
    chunk_id: uuid.UUID | None
    chunk_ordinal: int | None


async def list_conversations(
    session: AsyncSession, limit: int, offset: int
) -> tuple[list[tuple[Conversation, int]], int]:
    counts = (
        select(Message.conversation_id, func.count().label("message_count"))
        .group_by(Message.conversation_id)
        .subquery()
    )
    rows = (
        await session.execute(
            select(Conversation, func.coalesce(counts.c.message_count, 0))
            .outerjoin(counts, counts.c.conversation_id == Conversation.id)
            .order_by(Conversation.updated_at.desc(), Conversation.id.desc())
            .limit(limit)
            .offset(offset)
        )
    ).all()
    total = await session.scalar(select(func.count()).select_from(Conversation)) or 0
    return [(row[0], int(row[1])) for row in rows], total


async def load_messages(
    session: AsyncSession, conversation_id: uuid.UUID, limit: int
) -> tuple[list[Message], dict[uuid.UUID, list[MessageCitation]]]:
    """The most recent ``limit`` messages, oldest first, with their citations."""
    messages = list(
        (
            await session.scalars(
                select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.sequence.desc())
                .limit(limit)
            )
        ).all()
    )
    messages.reverse()
    citations: dict[uuid.UUID, list[MessageCitation]] = {m.id: [] for m in messages}
    if messages:
        rows = (
            await session.scalars(
                select(MessageCitation)
                .where(MessageCitation.message_id.in_(list(citations)))
                .order_by(MessageCitation.message_id, MessageCitation.source_index)
            )
        ).all()
        for citation in rows:
            citations[citation.message_id].append(citation)
    return messages, citations


async def locate_sources(
    session: AsyncSession, citations: list[MessageCitation]
) -> dict[uuid.UUID, SourceLocation]:
    """Resolve each citation to the chunk that holds its passage now (keyed by citation ID)."""
    if not citations:
        return {}
    chunk_ids = list({c.chunk_id for c in citations})
    document_ids = list({c.document_id for c in citations})
    live_chunks = {
        row.id: row.ordinal
        for row in (
            await session.execute(
                text("SELECT id, ordinal FROM chunks WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": chunk_ids},
            )
        ).all()
    }
    live_documents = set(
        (
            await session.execute(
                text("SELECT id FROM documents WHERE id = ANY(CAST(:ids AS uuid[]))"),
                {"ids": document_ids},
            )
        ).scalars()
    )
    missing = [
        c for c in citations if c.chunk_id not in live_chunks and c.document_id in live_documents
    ]
    relocated: dict[tuple[uuid.UUID, str], tuple[uuid.UUID, int]] = {}
    if missing:
        rows = (
            await session.execute(
                text(
                    "SELECT id, document_id, ordinal, "
                    "encode(sha256(convert_to(text, 'UTF8')), 'hex') AS digest FROM chunks "
                    "WHERE document_id = ANY(CAST(:documents AS uuid[])) "
                    "AND encode(sha256(convert_to(text, 'UTF8')), 'hex') = "
                    "ANY(CAST(:digests AS text[])) ORDER BY ordinal"
                ),
                {
                    "documents": list({c.document_id for c in missing}),
                    "digests": list({c.chunk_text_sha256 for c in missing}),
                },
            )
        ).all()
        for row in rows:
            relocated.setdefault((row.document_id, row.digest), (row.id, row.ordinal))

    locations: dict[uuid.UUID, SourceLocation] = {}
    for citation in citations:
        if citation.chunk_id in live_chunks:
            locations[citation.id] = SourceLocation(
                SourceStatus.AVAILABLE, citation.chunk_id, live_chunks[citation.chunk_id]
            )
        elif citation.document_id not in live_documents:
            locations[citation.id] = SourceLocation(SourceStatus.DELETED, None, None)
        elif (citation.document_id, citation.chunk_text_sha256) in relocated:
            chunk_id, ordinal = relocated[(citation.document_id, citation.chunk_text_sha256)]
            locations[citation.id] = SourceLocation(SourceStatus.RELOCATED, chunk_id, ordinal)
        else:
            locations[citation.id] = SourceLocation(SourceStatus.CHANGED, None, None)
    return locations
