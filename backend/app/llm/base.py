"""Provider-neutral interface for text generation.

The chat pipeline only talks to :class:`LLMProvider`. A provider turns a
:class:`GenerationRequest` into a stream of :class:`TextDelta` events followed by exactly one
:class:`GenerationDone`, or raises a :class:`ProviderError`. Adding OpenAI, Bedrock or a local
model means writing another implementation of this interface; nothing in the chat pipeline
changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal


@dataclass(frozen=True)
class PromptMessage:
    role: Literal["user", "assistant"]
    content: str


@dataclass(frozen=True)
class GenerationRequest:
    system: str
    messages: tuple[PromptMessage, ...]
    max_output_tokens: int


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_input_tokens: int = 0
    cache_creation_input_tokens: int = 0


class StopReason(StrEnum):
    """Why generation ended, in provider-neutral terms."""

    END = "end"  # the model finished its answer
    MAX_TOKENS = "max_tokens"  # cut off at the output limit
    REFUSAL = "refusal"  # the model or a safety classifier declined
    OTHER = "other"


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class GenerationDone:
    stop_reason: StopReason
    usage: Usage
    # The model that produced the answer. It can differ from the requested model when the
    # provider fell back to another model.
    model: str
    provider_request_id: str | None = None
    raw_stop_reason: str | None = None
    fell_back: bool = False


GenerationEvent = TextDelta | GenerationDone


@dataclass(frozen=True)
class GenerationResult:
    text: str
    done: GenerationDone


class ProviderErrorCode(StrEnum):
    RATE_LIMITED = "provider_rate_limited"
    OVERLOADED = "provider_overloaded"
    TIMEOUT = "provider_timeout"
    CONNECTION = "provider_connection_error"
    AUTHENTICATION = "provider_authentication_failed"
    PERMISSION = "provider_permission_denied"
    BAD_REQUEST = "provider_bad_request"
    MODEL_NOT_FOUND = "provider_model_not_found"
    REQUEST_TOO_LARGE = "provider_request_too_large"
    SERVER_ERROR = "provider_server_error"
    INVALID_RESPONSE = "provider_invalid_response"


@dataclass
class ProviderError(Exception):
    """A provider failure, with a message that is safe to show to users.

    ``message`` never contains the provider's raw error text (it can echo request content)
    or credentials; ``detail`` is for logs only.
    """

    code: ProviderErrorCode
    message: str
    retryable: bool
    retry_after_seconds: float | None = None
    status_code: int | None = None
    provider_request_id: str | None = None
    detail: str | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        super().__init__(self.message)


class LLMProvider(ABC):
    """A text-generation backend. One instance is shared by all requests."""

    name: str
    model: str

    @abstractmethod
    def stream(self, request: GenerationRequest) -> AsyncIterator[GenerationEvent]:
        """Yield text deltas, then one GenerationDone. Raise ProviderError on failure.

        Closing the iterator early (``aclose()``, or cancelling the task consuming it) must
        cancel the underlying request.
        """

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        """Generate a complete answer (non-streaming callers). Built on :meth:`stream`."""
        parts: list[str] = []
        done: GenerationDone | None = None
        stream = self.stream(request)
        try:
            async for event in stream:
                if isinstance(event, TextDelta):
                    parts.append(event.text)
                else:
                    done = event
        finally:
            await stream.aclose()  # type: ignore[attr-defined]
        if done is None:
            raise ProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "The model's response ended unexpectedly.",
                retryable=True,
            )
        return GenerationResult("".join(parts), done)

    async def aclose(self) -> None:  # noqa: B027  # optional hook
        """Release network resources at shutdown."""
