"""Anthropic Claude provider, using the official ``anthropic`` SDK.

One ``AsyncAnthropic`` client is created at startup and shared by every request (it pools
HTTP connections). Answers are always streamed. The SDK retries connection errors, 408, 409,
429 and 5xx responses before any output arrives (``LLM_MAX_RETRIES``); a failure after the
first token is reported, not retried, because the user has already seen part of the answer.

Thinking: current Claude models think adaptively and this provider leaves that at the model's
default. Thinking blocks are never shown, logged or stored, only the answer text.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

import anthropic

from app.core.config import Settings
from app.llm.base import (
    GenerationDone,
    GenerationEvent,
    GenerationRequest,
    LLMProvider,
    ProviderError,
    ProviderErrorCode,
    StopReason,
    TextDelta,
    Usage,
)

logger = logging.getLogger(__name__)

# Beta header for the scalar form ``fallbacks: "default"``.
FALLBACK_BETA = "server-side-fallback-2026-07-01"

_STOP_REASONS = {
    "end_turn": StopReason.END,
    "stop_sequence": StopReason.END,
    "max_tokens": StopReason.MAX_TOKENS,
    "refusal": StopReason.REFUSAL,
}


def _retry_after(error: anthropic.APIStatusError) -> float | None:
    value = error.response.headers.get("retry-after") if error.response is not None else None
    try:
        return max(0.0, float(value)) if value is not None else None
    except ValueError:
        return None


def map_error(error: Exception) -> ProviderError:
    """Translate an SDK exception into a ProviderError with a user-safe message."""
    if isinstance(error, anthropic.APITimeoutError):
        return ProviderError(
            ProviderErrorCode.TIMEOUT, "The language model did not respond in time.", True
        )
    if isinstance(error, anthropic.APIConnectionError):
        return ProviderError(
            ProviderErrorCode.CONNECTION, "The language model service could not be reached.", True
        )
    if isinstance(error, anthropic.APIStatusError):
        request_id = getattr(error, "request_id", None)
        status = error.status_code
        error_type = getattr(error, "type", None)
        # Mid-stream errors arrive on an HTTP 200 response, so the error type decides.
        if isinstance(error, anthropic.RateLimitError) or error_type == "rate_limit_error":
            code, message, retryable = (
                ProviderErrorCode.RATE_LIMITED,
                "The language model's rate limit was reached. Try again shortly.",
                True,
            )
        elif (
            isinstance(error, anthropic.OverloadedError)
            or error_type == "overloaded_error"
            or status == 529
        ):
            code, message, retryable = (
                ProviderErrorCode.OVERLOADED,
                "The language model is temporarily overloaded. Try again shortly.",
                True,
            )
        elif isinstance(error, anthropic.AuthenticationError):
            code, message, retryable = (
                ProviderErrorCode.AUTHENTICATION,
                "The server's language model credentials were rejected.",
                False,
            )
        elif isinstance(error, anthropic.PermissionDeniedError) or status == 402:
            code, message, retryable = (
                ProviderErrorCode.PERMISSION,
                "The server's language model account cannot use this model.",
                False,
            )
        elif isinstance(error, anthropic.NotFoundError):
            code, message, retryable = (
                ProviderErrorCode.MODEL_NOT_FOUND,
                "The configured language model was not found.",
                False,
            )
        elif isinstance(error, anthropic.RequestTooLargeError):
            code, message, retryable = (
                ProviderErrorCode.REQUEST_TOO_LARGE,
                "The question and its context are too large for the language model.",
                False,
            )
        elif isinstance(error, anthropic.BadRequestError | anthropic.UnprocessableEntityError):
            code, message, retryable = (
                ProviderErrorCode.BAD_REQUEST,
                "The language model rejected the request. Check the model settings.",
                False,
            )
        else:
            code, message, retryable = (
                ProviderErrorCode.SERVER_ERROR,
                "The language model service returned an error. Try again shortly.",
                status >= 500 or status == 200,
            )
        return ProviderError(
            code,
            message,
            retryable,
            retry_after_seconds=_retry_after(error),
            status_code=status,
            provider_request_id=request_id,
            detail=f"{type(error).__name__} {status} {error_type}",
        )
    return ProviderError(
        ProviderErrorCode.INVALID_RESPONSE,
        "The language model returned an unexpected response.",
        True,
        detail=type(error).__name__,
    )


class AnthropicProvider(LLMProvider):
    name = "anthropic"

    def __init__(self, settings: Settings, client: anthropic.AsyncAnthropic | None = None) -> None:
        self.model = settings.llm_model
        self.temperature = settings.llm_temperature
        self.effort = settings.llm_effort
        self.fallbacks = settings.llm_fallbacks
        if client is None:
            if settings.anthropic_api_key is None:
                raise ValueError("ANTHROPIC_API_KEY is not set")
            client = anthropic.AsyncAnthropic(
                api_key=settings.anthropic_api_key.get_secret_value(),
                timeout=anthropic.Timeout(
                    settings.llm_read_timeout_seconds,
                    connect=settings.llm_connect_timeout_seconds,
                ),
                max_retries=settings.llm_max_retries,
            )
        self._client = client

    def _params(self, request: GenerationRequest) -> dict[str, Any]:
        params: dict[str, Any] = {
            "model": self.model,
            "max_tokens": request.max_output_tokens,
            "system": request.system,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
        }
        if self.temperature is not None:
            # The SDK's typed parameters no longer include temperature (current models reject
            # it), so it is sent as an extra body field for models that still accept it.
            params["extra_body"] = {"temperature": self.temperature}
        if self.effort is not None:
            params["output_config"] = {"effort": self.effort}
        if self.fallbacks == "default":
            params["fallbacks"] = "default"
            params["betas"] = [FALLBACK_BETA]
        return params

    async def stream(self, request: GenerationRequest) -> AsyncIterator[GenerationEvent]:
        params = self._params(request)
        try:
            async with self._client.beta.messages.stream(**params) as stream:
                async for event in stream:
                    if (
                        event.type == "content_block_delta"
                        and event.delta.type == "text_delta"
                        and event.delta.text
                    ):
                        yield TextDelta(event.delta.text)
                message = await stream.get_final_message()
                request_id = stream.request_id
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except anthropic.AnthropicError as exc:
            error = map_error(exc)
            logger.warning(
                "llm_request_failed",
                extra={
                    "provider": self.name,
                    "model": self.model,
                    "error_code": error.code.value,
                    "detail": error.detail,
                    "provider_request_id": error.provider_request_id,
                },
            )
            raise error from exc

        usage = message.usage
        fell_back = any(
            getattr(entry, "type", None) == "fallback_message"
            for entry in (getattr(usage, "iterations", None) or [])
        )
        raw_stop = message.stop_reason
        yield GenerationDone(
            stop_reason=_STOP_REASONS.get(raw_stop or "", StopReason.OTHER),
            usage=Usage(
                input_tokens=usage.input_tokens or 0,
                output_tokens=usage.output_tokens or 0,
                cache_read_input_tokens=usage.cache_read_input_tokens or 0,
                cache_creation_input_tokens=usage.cache_creation_input_tokens or 0,
            ),
            model=message.model,
            provider_request_id=request_id,
            raw_stop_reason=raw_stop,
            fell_back=fell_back,
        )

    async def aclose(self) -> None:
        await self._client.close()
