"""Ollama provider: local models over Ollama's HTTP API (``POST /api/chat``).

The base URL comes only from ``OLLAMA_BASE_URL`` and the model from the ``OLLAMA_MODELS``
allowlist; nothing a browser sends ends up in a URL. One ``httpx2.AsyncClient`` is shared
by every Ollama model. Answers are streamed as newline-delimited JSON.

Retries: connection errors and 429/500/502/503 responses are retried before the response
starts (``LLM_MAX_RETRIES``, exponential backoff, ``Retry-After`` honoured up to 30 s). Once
text has streamed, a failure is reported, not retried. A mid-stream failure arrives as an
``{"error": ...}`` line on an HTTP 200 response.

Ollama "cloud" models (listed with a ``remote_host``) run on ollama.com rather than on this
machine; they are refused unless ``OLLAMA_ALLOW_REMOTE_MODELS=true``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any

import httpx2

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

RETRY_STATUS_CODES = frozenset({408, 429, 500, 502, 503})
MAX_RETRY_AFTER_SECONDS = 30.0
_BACKOFF_SECONDS = 0.5

_STOP_REASONS = {"stop": StopReason.END, "length": StopReason.MAX_TOKENS}

_UNREACHABLE = (
    "Ollama is not running, or it cannot be reached from the API server at the configured "
    "OLLAMA_BASE_URL (from Docker on macOS, use http://host.docker.internal:11434)."
)


def _retry_after(response: httpx2.Response) -> float | None:
    value = response.headers.get("retry-after")
    try:
        return min(MAX_RETRY_AFTER_SECONDS, max(0.0, float(value))) if value else None
    except ValueError:
        return None


def status_error(status: int, retry_after: float | None = None) -> ProviderError:
    """A ProviderError for an HTTP error status (Ollama's error text is not used)."""
    if status == 404:
        return ProviderError(
            ProviderErrorCode.MODEL_NOT_FOUND,
            "The selected model is not installed on the Ollama server (run `ollama pull`).",
            False,
            status_code=status,
            detail=f"HTTP {status}",
        )
    if status == 429:
        code, message, retryable = (
            ProviderErrorCode.RATE_LIMITED,
            "Ollama is busy (too many requests). Try again shortly.",
            True,
        )
    elif status in (401, 403):
        code, message, retryable = (
            ProviderErrorCode.PERMISSION,
            "The Ollama server refused the request.",
            False,
        )
    elif status == 413:
        code, message, retryable = (
            ProviderErrorCode.REQUEST_TOO_LARGE,
            "The question and its context are too large for the Ollama server.",
            False,
        )
    elif 400 <= status < 500:
        code, message, retryable = (
            ProviderErrorCode.BAD_REQUEST,
            "The Ollama server rejected the request. Check the model settings.",
            False,
        )
    elif status in (502, 503):
        code, message, retryable = (
            ProviderErrorCode.OVERLOADED,
            "The Ollama server is temporarily unavailable. Try again shortly.",
            True,
        )
    else:
        code, message, retryable = (
            ProviderErrorCode.SERVER_ERROR,
            "The Ollama server failed to generate an answer.",
            True,
        )
    return ProviderError(
        code,
        message,
        retryable,
        retry_after_seconds=retry_after,
        status_code=status,
        detail=f"HTTP {status}",
    )


def transport_error(error: httpx2.HTTPError) -> ProviderError:
    if isinstance(error, httpx2.TimeoutException):
        return ProviderError(
            ProviderErrorCode.TIMEOUT,
            "The local model did not respond in time (it may still be loading).",
            True,
            detail=type(error).__name__,
        )
    return ProviderError(
        ProviderErrorCode.CONNECTION, _UNREACHABLE, True, detail=type(error).__name__
    )


def create_client(settings: Settings, **kwargs: Any) -> httpx2.AsyncClient:
    if not settings.ollama_configured or settings.ollama_base_url is None:
        raise ValueError("OLLAMA_BASE_URL and OLLAMA_MODELS are not set")
    return httpx2.AsyncClient(
        base_url=settings.ollama_base_url,
        timeout=httpx2.Timeout(
            settings.llm_read_timeout_seconds, connect=settings.llm_connect_timeout_seconds
        ),
        # The base URL is fixed configuration; never follow a redirect elsewhere.
        follow_redirects=False,
        **kwargs,
    )


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(
        self,
        settings: Settings,
        client: httpx2.AsyncClient | None = None,
        *,
        model: str | None = None,
        owns_client: bool = True,
        sleep: Any = asyncio.sleep,
    ) -> None:
        self.model = model or settings.first_model("ollama")
        self.settings = settings
        self.max_retries = settings.llm_max_retries
        self._client = client if client is not None else create_client(settings)
        self._owns_client = owns_client
        self._sleep = sleep

    def _body(self, request: GenerationRequest) -> dict[str, Any]:
        settings = self.settings
        options: dict[str, Any] = {
            "num_predict": request.max_output_tokens,
            "num_ctx": settings.ollama_num_ctx,
        }
        if settings.llm_temperature is not None:
            options["temperature"] = settings.llm_temperature
        body: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": request.system},
                *({"role": m.role, "content": m.content} for m in request.messages),
            ],
            "stream": True,
            "options": options,
        }
        if settings.ollama_think is not None:
            body["think"] = settings.ollama_think
        if settings.ollama_keep_alive is not None:
            body["keep_alive"] = settings.ollama_keep_alive
        return body

    async def _open(self, body: dict[str, Any]) -> httpx2.Response:
        """Send the request, retrying transient failures before the response starts."""
        attempt = 0
        while True:
            try:
                request = self._client.build_request("POST", "/api/chat", json=body)
                response = await self._client.send(request, stream=True)
            except (httpx2.ConnectError, httpx2.ConnectTimeout) as exc:
                if attempt >= self.max_retries:
                    raise transport_error(exc) from exc
                delay = _BACKOFF_SECONDS * 2**attempt
            except httpx2.HTTPError as exc:
                raise transport_error(exc) from exc
            else:
                if response.status_code == 200:
                    return response
                retry_after = _retry_after(response)
                await response.aclose()
                if response.status_code not in RETRY_STATUS_CODES or attempt >= self.max_retries:
                    raise status_error(response.status_code, retry_after)
                delay = retry_after if retry_after is not None else _BACKOFF_SECONDS * 2**attempt
            attempt += 1
            logger.info(
                "llm_request_retry",
                extra={"provider": self.name, "model": self.model, "attempt": attempt},
            )
            await self._sleep(delay)

    async def stream(self, request: GenerationRequest) -> AsyncIterator[GenerationEvent]:
        final: dict[str, Any] | None = None
        try:
            response = await self._open(self._body(request))
            try:
                async for line in response.aiter_lines():
                    if not line.strip():
                        continue
                    try:
                        chunk = json.loads(line)
                    except json.JSONDecodeError as exc:
                        raise ProviderError(
                            ProviderErrorCode.INVALID_RESPONSE,
                            "The Ollama server returned an unexpected response.",
                            True,
                            detail="invalid JSON line",
                        ) from exc
                    if not isinstance(chunk, dict):
                        raise ProviderError(
                            ProviderErrorCode.INVALID_RESPONSE,
                            "The Ollama server returned an unexpected response.",
                            True,
                            detail="non-object line",
                        )
                    if "error" in chunk:
                        # A failure after the response started (HTTP status stays 200).
                        raise ProviderError(
                            ProviderErrorCode.SERVER_ERROR,
                            "The local model failed while generating the answer.",
                            True,
                            status_code=200,
                            detail="mid-stream error",
                        )
                    message = chunk.get("message")
                    text = message.get("content") if isinstance(message, dict) else None
                    # message.thinking (reasoning models) is never passed on.
                    if isinstance(text, str) and text:
                        yield TextDelta(text)
                    if chunk.get("done") is True:
                        final = chunk
                        break
            finally:
                await response.aclose()
        except (asyncio.CancelledError, GeneratorExit):
            raise
        except httpx2.HTTPError as exc:
            error = transport_error(exc)
            self._log(error)
            raise error from exc
        except ProviderError as error:
            self._log(error)
            raise

        if final is None:
            error = ProviderError(
                ProviderErrorCode.INVALID_RESPONSE,
                "The Ollama server's response ended unexpectedly.",
                True,
                detail="no final chunk",
            )
            self._log(error)
            raise error
        raw_stop = final.get("done_reason")
        raw_stop = raw_stop if isinstance(raw_stop, str) else None
        yield GenerationDone(
            stop_reason=_STOP_REASONS.get(raw_stop or "", StopReason.OTHER),
            usage=Usage(
                input_tokens=_int(final.get("prompt_eval_count")),
                output_tokens=_int(final.get("eval_count")),
            ),
            model=str(final.get("model") or self.model),
            raw_stop_reason=raw_stop,
        )

    def _log(self, error: ProviderError) -> None:
        logger.warning(
            "llm_request_failed",
            extra={
                "provider": self.name,
                "model": self.model,
                "error_code": error.code.value,
                "detail": error.detail,
            },
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()


def _int(value: object) -> int:
    return value if isinstance(value, int) and value >= 0 else 0


def installed_name(model: str, installed: dict[str, dict[str, Any]]) -> str | None:
    """The installed tag matching a configured name ("qwen3" matches "qwen3:latest")."""
    if model in installed:
        return model
    if ":" not in model and f"{model}:latest" in installed:
        return f"{model}:latest"
    return None


class OllamaBackend(ProviderBackend):
    """Local models served by Ollama."""

    name = "ollama"
    label = "Ollama (local)"

    def __init__(self, settings: Settings, client: httpx2.AsyncClient | None = None) -> None:
        super().__init__(settings.provider_models("ollama"))
        self.settings = settings
        self._configured = client is not None or settings.ollama_configured
        self._client = client

    @property
    def configured(self) -> bool:
        return self._configured

    @property
    def cost(self) -> Cost:
        return Cost.LOCAL

    @property
    def not_configured_reason(self) -> str | None:
        if self._configured:
            return None
        if self.settings.ollama_base_url is None:
            return "OLLAMA_BASE_URL is not set"
        return "OLLAMA_MODELS is empty"

    @property
    def notice(self) -> str | None:
        return (
            "Runs on the server's own machine: no per-question cost, and prompts stay on "
            "that machine. Answers are slower and usually less accurate than hosted models."
        )

    def _shared_client(self) -> httpx2.AsyncClient:
        if self._client is None:
            self._client = create_client(self.settings)
        return self._client

    def _create(self, model: str) -> LLMProvider:
        return OllamaProvider(self.settings, self._shared_client(), model=model, owns_client=False)

    async def check(self) -> dict[str, Availability]:
        """Compare the allowlist with the models installed in Ollama (``GET /api/tags``)."""
        try:
            response = await self._shared_client().get(
                "/api/tags", timeout=self.settings.llm_catalog_check_timeout_seconds
            )
        except httpx2.HTTPError as exc:
            logger.info(
                "llm_model_check_failed",
                extra={"provider": self.name, "detail": type(exc).__name__},
            )
            return {m: Availability.unavailable(_UNREACHABLE, transient=True) for m in self.models}
        try:
            payload = response.json() if response.status_code == 200 else None
            entries = payload.get("models") if isinstance(payload, dict) else None
            if not isinstance(entries, list):
                raise ValueError("no model list")
        except ValueError:
            logger.info(
                "llm_model_check_failed",
                extra={"provider": self.name, "detail": f"HTTP {response.status_code}"},
            )
            return {
                m: Availability.unknown("The Ollama server returned an unexpected model list.")
                for m in self.models
            }
        installed = {
            str(entry.get("name") or entry.get("model")): entry
            for entry in entries
            if isinstance(entry, dict) and (entry.get("name") or entry.get("model"))
        }
        results: dict[str, Availability] = {}
        for model in self.models:
            name = installed_name(model, installed)
            if name is None:
                results[model] = Availability.unavailable(
                    f"Not installed in Ollama. Run `ollama pull {model}` on the Ollama host."
                )
            elif (
                installed[name].get("remote_host") and not self.settings.ollama_allow_remote_models
            ):
                results[model] = Availability.unavailable(
                    "This is an Ollama cloud model: it runs on ollama.com, not locally. Set "
                    "OLLAMA_ALLOW_REMOTE_MODELS=true to allow it."
                )
            else:
                results[model] = Availability.available()
        return results

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
