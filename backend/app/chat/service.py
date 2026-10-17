"""The chat pipeline: one question in, one grounded and cited answer out.

Steps for each question:

1. validate the question (API layer) and the conversation's state;
2. store the question and a ``streaming`` answer placeholder in one transaction, and read
   the conversation history;
3. retrieve passages with the Phase 3 search service (semantic by default; keyword when the
   embedding model is unavailable);
4. select and de-duplicate sources within the token budget;
5. build the prompt (``app/chat/prompt.py``);
6. stream the answer from the provider, validating citation markers as text arrives;
7. classify the citations (supporting vs. context only) and the answer's grounding;
8. store the answer, its citations, usage and timings in one transaction.

No database connection is held while waiting for the model. If the client disconnects, the
generation task is cancelled, the provider request is closed and the partial answer is
stored as ``cancelled``.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.chat.citations import CitationFilter
from app.chat.context import SelectedSource, chunk_text_sha256, select_history, select_sources
from app.chat.events import (
    ChatEvent,
    CitationsChecked,
    Delta,
    Failed,
    Finished,
    SourceInfo,
    SourcesFound,
    Started,
    UsageReported,
)
from app.chat.prompt import SYSTEM_PROMPT, build_messages
from app.core.config import Settings
from app.db.models import (
    Conversation,
    Grounding,
    Language,
    Message,
    MessageCitation,
    MessageRole,
    MessageStatus,
)
from app.ingestion.language import detect_language
from app.llm.base import (
    GenerationDone,
    GenerationRequest,
    LLMProvider,
    ProviderError,
    ProviderErrorCode,
    StopReason,
    TextDelta,
    Usage,
)
from app.llm.catalog import ModelRef
from app.llm.registry import LLMRegistry, SelectionError
from app.search.retrieval import SearchFilters
from app.search.service import (
    QueryTooLongError,
    SearchMode,
    SearchOutcome,
    SearchRequest,
    SearchService,
    SearchUnavailableError,
)

logger = logging.getLogger(__name__)

DEFAULT_TITLE = "New conversation"

# Shown (and stored) when retrieval finds no passage at all. The model is not called, so
# nothing is generated; the notice says so plainly.
NO_SOURCES_NOTICE = {
    Language.JAPANESE: (
        "アップロードされた文書の中に、この質問に関連する箇所が見つかりませんでした。"
        "そのため、文書に基づいて回答することはできません。"
    ),
    Language.ENGLISH: (
        "No passage in the uploaded documents matched this question, so it cannot be "
        "answered from the documents."
    ),
}


class ChatError(Exception):
    """A request-level failure with an HTTP status and a user-safe message."""

    def __init__(
        self, status_code: int, code: str, message: str, retry_after: float | None = None
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.retry_after = retry_after


@dataclass(frozen=True)
class RetrievalOptions:
    mode: SearchMode | None = None
    document_ids: tuple[uuid.UUID, ...] | None = None
    language: str | None = None


@dataclass(frozen=True)
class AskRequest:
    conversation_id: uuid.UUID
    question: str
    client_message_id: uuid.UUID | None
    retrieval: RetrievalOptions
    request_id: str | None = None
    # The provider/model chosen for this answer; None uses the server's default.
    model: ModelRef | None = None


@dataclass(frozen=True)
class Replay:
    """A retried question whose answer is already complete."""

    user_message_id: uuid.UUID
    assistant_message_id: uuid.UUID


@dataclass
class _Turn:
    user_message_id: uuid.UUID
    assistant_message_id: uuid.UUID
    history: list[tuple[str, str]]


def _ms(since: float) -> float:
    return round((time.perf_counter() - since) * 1000, 2)


def title_from_question(question: str, max_chars: int) -> str:
    title = " ".join(question.split())
    if len(title) > max_chars:
        title = title[: max_chars - 1].rstrip() + "…"
    return title or DEFAULT_TITLE


def notice_language(language: Language) -> Language:
    return Language.JAPANESE if language is Language.JAPANESE else Language.ENGLISH


class ChatService:
    def __init__(
        self,
        settings: Settings,
        sessions: async_sessionmaker[AsyncSession],
        search: SearchService,
        llm: LLMRegistry | LLMProvider | None,
    ) -> None:
        self.settings = settings
        self.sessions = sessions
        self.search = search
        # A single provider (the evaluation harness, tests) becomes a one-model registry.
        self.registry = llm if isinstance(llm, LLMRegistry) else LLMRegistry.single(llm)
        # Answers being generated by this process (CHAT_MAX_CONCURRENT_GENERATIONS bounds it).
        self._busy = 0

    # --- request checks (no writes) -------------------------------------------------------

    async def check(self, session: AsyncSession, ask: AskRequest) -> Replay | None:
        """Fail fast with a ChatError, or return a Replay for an already answered retry.

        Everything checked here is checked again under a lock when the question is stored;
        this pass only lets the API answer with a plain JSON error instead of an event
        stream in the common cases.
        """
        conversation = await session.get(Conversation, ask.conversation_id)
        if conversation is None:
            raise ChatError(404, "conversation_not_found", "Conversation not found.")
        if ask.client_message_id is not None:
            existing = await self._find_question(session, ask)
            if existing is not None:
                answer = await self._answer_to(session, existing.id)
                if answer is not None and answer.status == MessageStatus.COMPLETE:
                    return Replay(existing.id, answer.id)
        try:
            await self.registry.select(ask.model)
        except SelectionError as exc:
            raise ChatError(exc.status_code, exc.code, exc.message) from exc
        streaming = await self._streaming_answer(session, ask.conversation_id)
        if streaming is not None and streaming.created_at >= self._stale_cutoff():
            raise ChatError(
                409,
                "conversation_busy",
                "An answer is still being generated in this conversation. Wait for it to "
                "finish or stop it first.",
            )
        if self._busy >= self.settings.chat_max_concurrent_generations:
            raise ChatError(
                429,
                "too_many_generations",
                "The server is answering too many questions at once. Try again shortly.",
                retry_after=5,
            )
        return None

    async def _find_question(self, session: AsyncSession, ask: AskRequest) -> Message | None:
        question = await session.scalar(
            select(Message).where(
                Message.conversation_id == ask.conversation_id,
                Message.client_message_id == ask.client_message_id,
            )
        )
        if question is not None and question.content != ask.question:
            raise ChatError(
                409,
                "client_message_id_reused",
                "This client_message_id was already used for a different question.",
            )
        return question

    @staticmethod
    async def _answer_to(session: AsyncSession, question_id: uuid.UUID) -> Message | None:
        return await session.scalar(select(Message).where(Message.reply_to_id == question_id))

    @staticmethod
    async def _streaming_answer(
        session: AsyncSession, conversation_id: uuid.UUID
    ) -> Message | None:
        return await session.scalar(
            select(Message).where(
                Message.conversation_id == conversation_id,
                Message.status == MessageStatus.STREAMING,
            )
        )

    # --- answering ------------------------------------------------------------------------

    async def answer(self, ask: AskRequest) -> AsyncIterator[ChatEvent]:
        """Run the pipeline, yielding events. Never raises for expected failures."""
        try:
            provider = self.registry.resolve(ask.model)
        except SelectionError as exc:
            yield Failed(exc.code, exc.message, False)
            return
        if self._busy >= self.settings.chat_max_concurrent_generations:
            yield Failed(
                "too_many_generations",
                "The server is answering too many questions at once. Try again shortly.",
                True,
                retry_after_seconds=5,
            )
            return
        self._busy += 1
        try:
            async for event in self._answer(ask, provider):
                yield event
        finally:
            self._busy -= 1

    async def _answer(self, ask: AskRequest, provider: LLMProvider) -> AsyncIterator[ChatEvent]:
        started = time.perf_counter()
        try:
            turn = await self._store_question(ask, provider)
        except ChatError as exc:
            yield Failed(exc.code, exc.message, exc.status_code in (409, 429))
            return

        yield Started(
            ask.conversation_id,
            turn.user_message_id,
            turn.assistant_message_id,
            provider.name,
            provider.model,
        )
        result = _AnswerState(turn.assistant_message_id, provider, started)
        try:
            async for event in self._generate(ask, turn, provider, result):
                yield event
        except (asyncio.CancelledError, GeneratorExit):
            result.status = MessageStatus.CANCELLED
            result.error_code = "cancelled"
            result.error_message = "The answer was stopped before it finished."
            logger.info(
                "chat_answer_cancelled",
                extra={"message_id": str(turn.assistant_message_id), "chars": len(result.text)},
            )
            await asyncio.shield(self._finish(ask, result))
            raise
        except Exception:
            logger.exception("chat_answer_failed", extra={"message_id": str(result.message_id)})
            result.status = MessageStatus.FAILED
            result.error_code = "internal_error"
            result.error_message = "The answer could not be completed because of a server error."
            await self._finish(ask, result)
            yield Failed(result.error_code, result.error_message, True, result.message_id)
            return

        await self._finish(ask, result)
        if result.status == MessageStatus.FAILED:
            yield Failed(
                result.error_code or "provider_error",
                result.error_message or "The answer could not be completed.",
                result.retryable,
                result.message_id,
                result.retry_after,
            )
            return
        yield CitationsChecked(result.citations(), result.invalid_markers)
        if result.usage is not None:
            yield UsageReported(result.usage)
        yield Finished(
            result.message_id,
            result.status.value,
            result.grounding.value if result.grounding else None,
            result.stop_reason,
            result.served_model,
            result.timings(),
        )

    async def _generate(
        self, ask: AskRequest, turn: _Turn, provider: LLMProvider, result: _AnswerState
    ) -> AsyncIterator[ChatEvent]:
        settings = self.settings
        question_language = detect_language(ask.question)

        # Retrieval: follow-ups that are too short to search well on their own are searched
        # together with the previous question.
        query = ask.question
        if turn.history and len(ask.question) < settings.chat_followup_max_chars:
            query = f"{turn.history[-1][0]}\n{ask.question}"
        query = query[: settings.search_max_query_chars]
        step = time.perf_counter()
        outcome = await self._retrieve(query, ask.retrieval)
        result.retrieval_ms = _ms(step)
        result.retrieval_mode = outcome.mode.value
        sources = select_sources(outcome.hits, settings.chat_context_token_budget)
        result.sources = [self._source_info(source, outcome.score_type.value) for source in sources]
        result.source_hashes = {s.index: chunk_text_sha256(s.hit.chunk.text) for s in sources}
        yield SourcesFound(
            outcome.mode.value,
            query,
            outcome.semantic_available,
            outcome.warnings,
            result.retrieval_ms,
            result.sources,
        )

        if not sources:
            notice = NO_SOURCES_NOTICE[notice_language(question_language)]
            result.text = notice
            result.grounding = Grounding.NO_SOURCES
            result.status = MessageStatus.COMPLETE
            result.provider_used = False
            yield Delta(notice)
            return

        history = select_history(
            turn.history, settings.chat_history_token_budget, settings.chat_history_max_messages
        )
        request = GenerationRequest(
            system=SYSTEM_PROMPT,
            messages=build_messages(
                history, ask.question, [s.prompt_source() for s in sources], question_language
            ),
            max_output_tokens=settings.llm_max_output_tokens,
        )
        citation_filter = CitationFilter(len(sources))
        result.citation_filter = citation_filter
        deadline = asyncio.get_running_loop().time() + settings.llm_request_timeout_seconds
        stream = provider.stream(request)
        done: GenerationDone | None = None
        try:
            while True:
                try:
                    async with asyncio.timeout_at(deadline):
                        event = await anext(stream)
                except StopAsyncIteration:
                    break
                except TimeoutError:
                    raise ProviderError(
                        ProviderErrorCode.TIMEOUT,
                        "The language model did not finish the answer in time.",
                        retryable=True,
                    ) from None
                if isinstance(event, TextDelta):
                    if result.first_token_ms is None:
                        result.first_token_ms = _ms(result.started)
                    safe = citation_filter.feed(event.text)
                    if safe:
                        result.text += safe
                        yield Delta(safe)
                else:
                    done = event
        except ProviderError as error:
            tail = citation_filter.flush()
            if tail:
                result.text += tail
                yield Delta(tail)
            result.status = MessageStatus.FAILED
            result.error_code = error.code.value
            result.error_message = error.message
            result.retryable = error.retryable
            result.retry_after = error.retry_after_seconds
            result.provider_request_id = error.provider_request_id
            return
        finally:
            await stream.aclose()  # type: ignore[attr-defined]

        tail = citation_filter.flush()
        if tail:
            result.text += tail
            yield Delta(tail)
        if done is None:
            result.status = MessageStatus.FAILED
            result.error_code = ProviderErrorCode.INVALID_RESPONSE.value
            result.error_message = "The language model's response ended unexpectedly."
            result.retryable = True
            return
        result.usage = done.usage
        result.served_model = done.model
        result.stop_reason = done.raw_stop_reason or done.stop_reason.value
        result.provider_request_id = done.provider_request_id
        if done.stop_reason is StopReason.REFUSAL:
            result.status = MessageStatus.FAILED
            result.error_code = "model_refused"
            result.error_message = "The language model declined to answer this question."
            result.retryable = False
            return
        result.status = MessageStatus.COMPLETE
        if citation_filter.abstained:
            result.grounding = Grounding.INSUFFICIENT_EVIDENCE
        elif citation_filter.cited:
            result.grounding = Grounding.GROUNDED
        else:
            result.grounding = Grounding.UNCITED

    async def _retrieve(self, query: str, options: RetrievalOptions) -> SearchOutcome:
        settings = self.settings
        mode = options.mode or SearchMode(
            settings.chat_retrieval_mode or settings.search_default_mode
        )
        request = SearchRequest(
            query=query,
            mode=mode,
            top_k=settings.chat_retrieval_top_k,
            filters=SearchFilters(document_ids=options.document_ids, language=options.language),
            min_similarity=settings.chat_min_similarity if mode is not SearchMode.KEYWORD else None,
        )
        async with self.sessions() as session:
            try:
                return await self.search.search(session, request)
            except (SearchUnavailableError, QueryTooLongError) as exc:
                if mode is SearchMode.KEYWORD:
                    raise
                # Answer from keyword results rather than not at all; the warning says so.
                reason = getattr(exc, "message", "the embedding model is unavailable")
                outcome = await self.search.search(
                    session, replace(request, mode=SearchMode.KEYWORD, min_similarity=None)
                )
                outcome.semantic_available = False
                outcome.warnings.insert(
                    0,
                    f"Semantic search is unavailable ({reason}); sources come from keyword "
                    "search only.",
                )
                return outcome

    def _source_info(self, source: SelectedSource, score_type: str) -> SourceInfo:
        chunk = source.hit.chunk
        limit = self.settings.chat_citation_excerpt_chars
        return SourceInfo(
            index=source.index,
            chunk_id=chunk.chunk_id,
            document_id=chunk.document_id,
            filename=chunk.filename,
            chunk_ordinal=chunk.ordinal,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            section=chunk.section,
            language=chunk.language,
            retrieval_rank=source.hit.rank,
            score=source.hit.score,
            score_type=score_type,
            excerpt=chunk.text[:limit] if limit else None,
        )

    # --- persistence ----------------------------------------------------------------------

    async def _store_question(self, ask: AskRequest, provider: LLMProvider) -> _Turn:
        settings = self.settings
        async with self.sessions() as session:
            try:
                conversation = await session.get(
                    Conversation, ask.conversation_id, with_for_update=True
                )
                if conversation is None:
                    raise ChatError(404, "conversation_not_found", "Conversation not found.")
                await self._expire_stale(session, ask.conversation_id)
                if await self._streaming_answer(session, ask.conversation_id) is not None:
                    raise ChatError(
                        409,
                        "conversation_busy",
                        "An answer is still being generated in this conversation.",
                    )

                question: Message | None = None
                answer_sequence: int | None = None
                if ask.client_message_id is not None:
                    question = await self._find_question(session, ask)
                if question is not None:
                    previous = await self._answer_to(session, question.id)
                    if previous is not None:
                        if previous.status == MessageStatus.COMPLETE:
                            raise ChatError(
                                409, "already_answered", "This question was already answered."
                            )
                        # Retrying a failed or stopped answer replaces it in place.
                        answer_sequence = previous.sequence
                        await session.execute(delete(Message).where(Message.id == previous.id))
                        await session.flush()
                    if answer_sequence is None:
                        answer_sequence = await self._next_sequence(session, ask.conversation_id)
                else:
                    sequence = await self._next_sequence(session, ask.conversation_id)
                    question = Message(
                        id=uuid.uuid4(),
                        conversation_id=ask.conversation_id,
                        sequence=sequence,
                        role=MessageRole.USER.value,
                        content=ask.question,
                        status=MessageStatus.COMPLETE.value,
                        client_message_id=ask.client_message_id,
                        request_id=ask.request_id,
                    )
                    session.add(question)
                    await session.flush()
                    answer_sequence = sequence + 1
                    if conversation.title_is_default:
                        conversation.title = title_from_question(
                            ask.question, min(settings.chat_max_title_chars, 200)
                        )
                        conversation.title_is_default = False

                answer = Message(
                    id=uuid.uuid4(),
                    conversation_id=ask.conversation_id,
                    sequence=answer_sequence,
                    role=MessageRole.ASSISTANT.value,
                    content="",
                    status=MessageStatus.STREAMING.value,
                    reply_to_id=question.id,
                    provider=provider.name,
                    model=provider.model,
                    requested_provider=provider.name,
                    requested_model=provider.model,
                    request_id=ask.request_id,
                )
                session.add(answer)
                conversation.updated_at = func.now()
                history = await self._history(session, ask.conversation_id, question.sequence)
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                logger.info("chat_question_conflict", extra={"error_type": type(exc).__name__})
                raise ChatError(
                    409,
                    "conversation_busy",
                    "Another question is being stored in this conversation. Try again.",
                ) from exc
        return _Turn(question.id, answer.id, history)

    def _stale_cutoff(self) -> datetime:
        """An answer still ``streaming`` after this long was abandoned by a stopped server."""
        return datetime.now(UTC) - timedelta(
            seconds=self.settings.llm_request_timeout_seconds + 120
        )

    async def _expire_stale(self, session: AsyncSession, conversation_id: uuid.UUID) -> None:
        """Mark answers left ``streaming`` by a crashed process as failed."""
        cutoff = self._stale_cutoff()
        result = await session.execute(
            update(Message)
            .where(
                Message.conversation_id == conversation_id,
                Message.status == MessageStatus.STREAMING,
                Message.created_at < cutoff,
            )
            .values(
                status=MessageStatus.FAILED.value,
                error_code="interrupted",
                error_message="The answer was interrupted (the server stopped).",
                completed_at=func.now(),
            )
        )
        if result.rowcount:  # type: ignore[attr-defined]
            logger.warning("chat_stale_answers_expired", extra={"count": result.rowcount})  # type: ignore[attr-defined]

    @staticmethod
    async def _next_sequence(session: AsyncSession, conversation_id: uuid.UUID) -> int:
        current = await session.scalar(
            select(func.max(Message.sequence)).where(Message.conversation_id == conversation_id)
        )
        return (current or 0) + 1

    @staticmethod
    async def _history(
        session: AsyncSession, conversation_id: uuid.UUID, before_sequence: int
    ) -> list[tuple[str, str]]:
        """Earlier questions with complete answers, oldest first."""
        rows = (
            await session.scalars(
                select(Message)
                .where(
                    Message.conversation_id == conversation_id,
                    Message.sequence < before_sequence,
                )
                .order_by(Message.sequence)
            )
        ).all()
        questions = {m.id: m for m in rows if m.role == MessageRole.USER}
        pairs = []
        for message in rows:
            if (
                message.role == MessageRole.ASSISTANT
                and message.status == MessageStatus.COMPLETE
                and message.reply_to_id in questions
                and message.grounding != Grounding.NO_SOURCES
            ):
                pairs.append((questions[message.reply_to_id].content, message.content))
        return pairs

    async def _finish(self, ask: AskRequest, result: _AnswerState) -> None:
        """Store the outcome. Errors are logged, never raised: the stream must still end."""
        result.total_ms = _ms(result.started)
        try:
            async with self.sessions() as session:
                stored = await session.execute(
                    update(Message)
                    .where(
                        Message.id == result.message_id,
                        Message.status == MessageStatus.STREAMING,
                    )
                    .values(
                        content=result.text,
                        status=result.status.value,
                        grounding=result.grounding.value if result.grounding else None,
                        error_code=result.error_code,
                        error_message=result.error_message,
                        provider=result.provider.name if result.provider_used else None,
                        model=result.provider.model if result.provider_used else None,
                        served_model=result.served_model,
                        stop_reason=result.stop_reason,
                        provider_request_id=result.provider_request_id,
                        input_tokens=result.usage.input_tokens if result.usage else None,
                        output_tokens=result.usage.output_tokens if result.usage else None,
                        cache_read_input_tokens=(
                            result.usage.cache_read_input_tokens if result.usage else None
                        ),
                        cache_creation_input_tokens=(
                            result.usage.cache_creation_input_tokens if result.usage else None
                        ),
                        retrieval_mode=result.retrieval_mode,
                        retrieval_ms=result.retrieval_ms,
                        first_token_ms=result.first_token_ms,
                        total_ms=result.total_ms,
                        completed_at=func.now(),
                    )
                )
                if not stored.rowcount:  # type: ignore[attr-defined]
                    # The conversation was deleted (or the answer expired) meanwhile.
                    await session.rollback()
                    logger.info(
                        "chat_answer_not_stored", extra={"message_id": str(result.message_id)}
                    )
                    return
                for source in result.citations():
                    session.add(
                        MessageCitation(
                            id=uuid.uuid4(),
                            message_id=result.message_id,
                            source_index=source.index,
                            supporting=source.supporting,
                            chunk_id=source.chunk_id,
                            document_id=source.document_id,
                            filename=source.filename,
                            chunk_ordinal=source.chunk_ordinal,
                            page_start=source.page_start,
                            page_end=source.page_end,
                            section=source.section,
                            language=source.language,
                            retrieval_rank=source.retrieval_rank,
                            score=source.score,
                            score_type=source.score_type,
                            chunk_text_sha256=result.source_hashes[source.index],
                            excerpt=source.excerpt,
                        )
                    )
                await session.execute(
                    update(Conversation)
                    .where(Conversation.id == ask.conversation_id)
                    .values(updated_at=func.now())
                )
                await session.commit()
        except Exception:
            logger.exception(
                "chat_answer_store_failed", extra={"message_id": str(result.message_id)}
            )
            return
        logger.info(
            "chat_answer_finished",
            extra={
                "message_id": str(result.message_id),
                "status": result.status.value,
                "grounding": result.grounding.value if result.grounding else None,
                "sources": len(result.sources),
                "cited": len(result.citation_filter.cited) if result.citation_filter else 0,
                "invalid_markers": result.invalid_markers,
                "input_tokens": result.usage.input_tokens if result.usage else None,
                "output_tokens": result.usage.output_tokens if result.usage else None,
                "served_model": result.served_model,
                **{key: value for key, value in result.timings().items()},
            },
        )


@dataclass
class _AnswerState:
    message_id: uuid.UUID
    provider: LLMProvider
    started: float
    text: str = ""
    status: MessageStatus = MessageStatus.FAILED
    grounding: Grounding | None = None
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
    retry_after: float | None = None
    usage: Usage | None = None
    served_model: str | None = None
    stop_reason: str | None = None
    provider_request_id: str | None = None
    provider_used: bool = True
    retrieval_mode: str | None = None
    retrieval_ms: float | None = None
    first_token_ms: float | None = None
    total_ms: float | None = None
    sources: list[SourceInfo] | None = None
    citation_filter: CitationFilter | None = None

    def __post_init__(self) -> None:
        self.sources = []
        self.source_hashes: dict[int, str] = {}

    @property
    def invalid_markers(self) -> int:
        return self.citation_filter.invalid_markers if self.citation_filter else 0

    def citations(self) -> list[SourceInfo]:
        cited = set(self.citation_filter.cited) if self.citation_filter else set()
        return [replace(source, supporting=source.index in cited) for source in self.sources or []]

    def timings(self) -> dict[str, float]:
        timings = {"total_ms": self.total_ms if self.total_ms is not None else _ms(self.started)}
        if self.retrieval_ms is not None:
            timings["retrieval_ms"] = self.retrieval_ms
        if self.first_token_ms is not None:
            timings["first_token_ms"] = self.first_token_ms
        return timings
