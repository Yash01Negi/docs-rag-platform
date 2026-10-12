"""Events produced while answering a question, independent of the wire format.

The API turns them into Server-Sent Events (``app/api/v1/conversations.py``); the evaluation
harness consumes them directly.
"""

import uuid
from dataclasses import dataclass, field

from app.llm.base import Usage


@dataclass(frozen=True)
class SourceInfo:
    index: int
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    filename: str
    chunk_ordinal: int
    page_start: int | None
    page_end: int | None
    section: str | None
    language: str
    retrieval_rank: int
    score: float
    score_type: str
    excerpt: str | None
    supporting: bool = False


@dataclass(frozen=True)
class Started:
    conversation_id: uuid.UUID
    user_message_id: uuid.UUID
    assistant_message_id: uuid.UUID
    provider: str | None
    model: str | None
    replayed: bool = False


@dataclass(frozen=True)
class SourcesFound:
    mode: str
    query: str
    semantic_available: bool
    warnings: list[str]
    retrieval_ms: float
    sources: list[SourceInfo]


@dataclass(frozen=True)
class Delta:
    text: str


@dataclass(frozen=True)
class CitationsChecked:
    citations: list[SourceInfo]  # every source, with ``supporting`` set for cited ones
    invalid_markers_removed: int


@dataclass(frozen=True)
class UsageReported:
    usage: Usage


@dataclass(frozen=True)
class Finished:
    assistant_message_id: uuid.UUID
    status: str
    grounding: str | None
    stop_reason: str | None
    served_model: str | None
    timings_ms: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Failed:
    code: str
    message: str
    retryable: bool
    assistant_message_id: uuid.UUID | None = None
    retry_after_seconds: float | None = None


ChatEvent = Started | SourcesFound | Delta | CitationsChecked | UsageReported | Finished | Failed
