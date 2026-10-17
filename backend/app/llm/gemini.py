"""Google Gemini provider, using the official Google Gen AI SDK (``google-genai``).

The Gemini Developer API is used with an API key (``GEMINI_API_KEY``); Vertex AI is never
selected, whatever the environment says. One ``genai.Client`` is created at startup and
shared by every Gemini model. Answers are streamed (``generate_content_stream``).

Retries: the SDK retries 408, 429 and 5xx responses and transient connection errors before
the response starts (``LLM_MAX_RETRIES``, exponential backoff); a failure after the first
token is reported, not retried. No other model is ever substituted for the selected one.

Thinking: Gemini 3 models think by default. Thought parts are never requested
(``include_thoughts`` is left unset) and are dropped if they arrive; thinking tokens are
billed as output tokens and are counted in ``output_tokens``.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

import httpx
from google import genai
from google.genai import errors, types

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
from app.llm.catalog import Availability, Cost
from app.llm.registry import ProviderBackend

logger = logging.getLogger(__name__)

# Status codes the SDK retries before the response starts.
RETRY_STATUS_CODES = [408, 429, 500, 502, 503, 504]

_STOP_REASONS = {
    "STOP": StopReason.END,
    "MAX_TOKENS": StopReason.MAX_TOKENS,
    # The answer was blocked by a safety, policy or recitation filter.
    "SAFETY": StopReason.REFUSAL,
    "RECITATION": StopReason.REFUSAL,
    "BLOCKLIST": StopReason.REFUSAL,
    "PROHIBITED_CONTENT": StopReason.REFUSAL,
    "SPII": StopReason.REFUSAL,
    "IMAGE_SAFETY": StopReason.REFUSAL,
    "IMAGE_PROHIBITED_CONTENT": StopReason.REFUSAL,
}

_RETRY_DELAY = re.compile(r"^(\d+(?:\.\d+)?)s$")


def _error_details(error: errors.APIError) -> list[dict[str, Any]]:
    """The google.rpc detail objects of an error response (ErrorInfo, RetryInfo, ...)."""
    body = error.details if isinstance(error.details, dict) else {}
    inner = body.get("error", body) if isinstance(body, dict) else {}
    details = inner.get("details") if isinstance(inner, dict) else None
    return [d for d in details if isinstance(d, dict)] if isinstance(details, list) else []


def _retry_after(error: errors.APIError) -> float | None:
    for detail in _error_details(error):
        if str(detail.get("@type", "")).endswith("google.rpc.RetryInfo"):
            match = _RETRY_DELAY.match(str(detail.get("retryDelay", "")))
            if match:
                return float(match.group(1))
    response = getattr(error, "response", None)
    value = getattr(response, "headers", {}).get("retry-after") if response is not None else None
    try:
        return max(0.0, float(value)) if value is not None else None
    except (TypeError, ValueError):
        return None


def _reasons(error: errors.APIError) -> set[str]:
    return {str(d.get("reason")) for d in _error_details(error) if d.get("reason")}


def map_error(error: Exception) -> ProviderError:
    """Translate an SDK or transport exception into a ProviderError with a user-safe message.

    The provider's own error text is never put in the message (it can echo request content);
    ``detail`` carries only the exception class, HTTP status and gRPC status for logs.
    """
    if isinstance(error, httpx.TimeoutException):
        return ProviderError(
            ProviderErrorCode.TIMEOUT,
            "Gemini did not respond in time.",
            True,
            detail=type(error).__name__,
        )
    if isinstance(error, httpx.TransportError):
        return ProviderError(
            ProviderErrorCode.CONNECTION,
            "The Gemini API could not be reached.",
            True,
            detail=type(error).__name__,
        )
    if isinstance(error, errors.APIError):
        code = int(error.code or 0)
        status = str(error.status or "")
        reasons = _reasons(error)
        if code == 429 or status == "RESOURCE_EXHAUSTED":
            result = (
                ProviderErrorCode.RATE_LIMITED,
                "Gemini's rate limit or quota was reached (free-tier limits are low). "
                "Try again later or choose another model.",
                True,
            )
        elif "API_KEY_INVALID" in reasons or code == 401 or status == "UNAUTHENTICATED":
            result = (
                ProviderErrorCode.AUTHENTICATION,
                "The server's Gemini API key was rejected.",
                False,
            )
        elif code == 403 or status == "PERMISSION_DENIED":
            result = (
                ProviderErrorCode.PERMISSION,
                "The server's Gemini API key cannot use this model.",
                False,
            )
        elif status == "FAILED_PRECONDITION":
            # For example: the free tier is not offered in the server's region.
            result = (
                ProviderErrorCode.PERMISSION,
                "Gemini is not available for the server's account or region.",
                False,
            )
        elif code == 404 or status == "NOT_FOUND":
            result = (
                ProviderErrorCode.MODEL_NOT_FOUND,
                "The selected Gemini model was not found.",
                False,
            )
        elif code == 413:
            result = (
                ProviderErrorCode.REQUEST_TOO_LARGE,
                "The question and its context are too large for Gemini.",
                False,
            )
        elif code == 503 or status == "UNAVAILABLE":
            result = (
                ProviderErrorCode.OVERLOADED,
                "Gemini is temporarily overloaded. Try again shortly.",
                True,
            )
        elif code == 504 or status == "DEADLINE_EXCEEDED":
            result = (ProviderErrorCode.TIMEOUT, "Gemini did not respond in time.", True)
        elif 400 <= code < 500:
            result = (
                ProviderErrorCode.BAD_REQUEST,
                "Gemini rejected the request. Check the model settings.",
                False,
            )
        else:
            result = (
                ProviderErrorCode.SERVER_ERROR,
                "The Gemini API returned an error. Try again shortly.",
                True,
            )
        return ProviderError(
            *result,
            retry_after_seconds=_retry_after(error),
            status_code=code or None,
            detail=f"{type(error).__name__} {code} {status}".strip(),
        )
    return ProviderError(
        ProviderErrorCode.INVALID_RESPONSE,
        "Gemini returned an unexpected response.",
        True,
        detail=type(error).__name__,
    )


def create_client(
    settings: Settings, *, retry_initial_delay: float | None = None, **http_options: Any
) -> genai.Client:
    """The shared client. ``retry_initial_delay`` and ``http_options`` are for tests."""
    if not settings.gemini_configured or settings.gemini_api_key is None:
        raise ValueError("GEMINI_API_KEY is not set")
    return genai.Client(
        vertexai=False,
        api_key=settings.gemini_api_key.get_secret_value(),
        http_options=types.HttpOptions(
            # Milliseconds; httpx applies it to connecting and to each read of the stream.
            timeout=int(settings.llm_read_timeout_seconds * 1000),
            retry_options=types.HttpRetryOptions(
                attempts=settings.llm_max_retries + 1,
                http_status_codes=RETRY_STATUS_CODES,
                initial_delay=retry_initial_delay,
            ),
            **http_options,
        ),
    )


class GeminiProvider(LLMProvider):
    name = "gemini"

    def __init__(
        self,
        settings: Settings,
        client: genai.Client | None = None,
        *,
        model: str | None = None,
        owns_client: bool = True,
    ) -> None:
        self.model = model or settings.first_model("gemini")
        self.temperature = settings.llm_temperature
        self.thinking_level = settings.gemini_thinking_level
        self._client = client if client is not None else create_client(settings)
        self._owns_client = owns_client

    def _config(self, request: GenerationRequest) -> types.GenerateContentConfig:
        config: dict[str, Any] = {
            "system_instruction": request.system,
            "max_output_tokens": request.max_output_tokens,
            "candidate_count": 1,
        }
        if self.temperature is not None:
            config["temperature"] = self.temperature
        if self.thinking_level is not None:
            config["thinking_config"] = types.ThinkingConfig(
                thinking_level=types.ThinkingLevel(self.thinking_level.upper())
            )
        return types.GenerateContentConfig(**config)

    @staticmethod
    def _contents(request: GenerationRequest) -> list[types.Content]:
        return [
            types.Content(
                role="user" if message.role == "user" else "model",
                parts=[types.Part(text=message.content)],
            )
            for message in request.messages
        ]

    async def stream(self, request: GenerationRequest) -> AsyncIterator[GenerationEvent]:
        finish_reason: str | None = None
        blocked = False
        usage_metadata: types.GenerateContentResponseUsageMetadata | None = None
        served_model: str | None = None
        response_id: str | None = None
        try:
            chunks = await self._client.aio.models.generate_content_stream(
                model=self.model, contents=self._contents(request), config=self._config(request)
            )
            try:
                async for chunk in chunks:
                    served_model = chunk.model_version or served_model
                    response_id = chunk.response_id or response_id
                    if chunk.usage_metadata is not None:
                        usage_metadata = chunk.usage_metadata
                    feedback = chunk.prompt_feedback
                    if feedback is not None and feedback.block_reason is not None:
                        blocked = True
                    candidate = chunk.candidates[0] if chunk.candidates else None
                    if candidate is None:
                        continue
                    if candidate.finish_reason is not None:
                        finish_reason = str(getattr(candidate.finish_reason, "value", ""))
                    content = candidate.content
                    for part in (content.parts if content is not None else None) or []:
                        # Thought parts (and any non-text part) are never passed on.
                        if part.text and not part.thought:
                            yield TextDelta(part.text)
            finally:
                await chunks.aclose()  # type: ignore[attr-defined]
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except (errors.APIError, httpx.HTTPError, ValueError) as exc:
            error = map_error(exc)
            logger.warning(
                "llm_request_failed",
                extra={
                    "provider": self.name,
                    "model": self.model,
                    "error_code": error.code.value,
                    "detail": error.detail,
                },
            )
            raise error from exc

        if blocked:
            stop, raw = StopReason.REFUSAL, "PROMPT_BLOCKED"
        elif finish_reason is None:
            raise ProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "Gemini's response ended without a finish reason.",
                True,
            )
        else:
            stop, raw = _STOP_REASONS.get(finish_reason, StopReason.OTHER), finish_reason
        meta = usage_metadata
        prompt = (meta.prompt_token_count or 0) if meta else 0
        cached = (meta.cached_content_token_count or 0) if meta else 0
        yield GenerationDone(
            stop_reason=stop,
            usage=Usage(
                # Gemini counts cached tokens inside the prompt; they are reported apart,
                # as Claude does.
                input_tokens=max(0, prompt - cached),
                output_tokens=(
                    ((meta.candidates_token_count or 0) + (meta.thoughts_token_count or 0))
                    if meta
                    else 0
                ),
                cache_read_input_tokens=cached,
            ),
            model=served_model or self.model,
            provider_request_id=response_id,
            raw_stop_reason=raw,
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aio.aclose()


class GeminiBackend(ProviderBackend):
    """Gemini models over one shared ``genai.Client`` (free tier or paid)."""

    name = "gemini"
    label = "Google Gemini"

    def __init__(self, settings: Settings, client: genai.Client | None = None) -> None:
        super().__init__(settings.provider_models("gemini"))
        self.settings = settings
        self._configured = client is not None or settings.gemini_configured
        self._client = client

    @property
    def configured(self) -> bool:
        return self._configured

    @property
    def cost(self) -> Cost:
        return Cost.FREE_TIER if self.settings.gemini_tier == "free" else Cost.PAID

    @property
    def not_configured_reason(self) -> str | None:
        return None if self._configured else "GEMINI_API_KEY is not set"

    @property
    def notice(self) -> str | None:
        if self.settings.gemini_tier == "free":
            return (
                "Free tier: low rate limits, and Google may use prompts and answers "
                "(including document passages) to improve its products. Do not use it for "
                "confidential documents."
            )
        return "Paid tier: each question is billed to the server's Google Cloud project."

    def _shared_client(self) -> genai.Client:
        if self._client is None:
            self._client = create_client(self.settings)
        return self._client

    def _create(self, model: str) -> LLMProvider:
        return GeminiProvider(self.settings, self._shared_client(), model=model, owns_client=False)

    async def check(self) -> dict[str, Availability]:
        """Look each configured model up in the Gemini API's model list (no tokens used)."""
        client = self._shared_client()
        options = types.HttpOptions(
            timeout=int(self.settings.llm_catalog_check_timeout_seconds * 1000),
            retry_options=types.HttpRetryOptions(attempts=1),
        )
        results: dict[str, Availability] = {}
        for model in self.models:
            try:
                await client.aio.models.get(
                    model=model, config=types.GetModelConfig(http_options=options)
                )
                results[model] = Availability.available()
            except (errors.APIError, httpx.HTTPError, ValueError) as exc:
                error = map_error(exc)
                logger.info(
                    "llm_model_check_failed",
                    extra={"provider": self.name, "model": model, "detail": error.detail},
                )
                if error.code is ProviderErrorCode.MODEL_NOT_FOUND:
                    results[model] = Availability.unavailable(
                        "Google does not list this model ID (it may be retired or misspelled)."
                    )
                elif error.code is ProviderErrorCode.AUTHENTICATION:
                    results[model] = Availability.unavailable(
                        "The server's Gemini API key was rejected."
                    )
                elif error.code is ProviderErrorCode.PERMISSION:
                    results[model] = Availability.unavailable(
                        "The server's Gemini API key cannot use this model."
                    )
                else:
                    results[model] = Availability.unknown("Gemini could not be checked.")
        return results

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aio.aclose()
