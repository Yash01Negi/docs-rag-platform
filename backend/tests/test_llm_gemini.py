"""Contract tests for the Gemini provider: the real Google Gen AI SDK against a mock transport.

No request leaves the process: an ``httpx.MockTransport`` answers with responses shaped
like the Gemini API's (``streamGenerateContent?alt=sse`` chunks and google.rpc error
bodies). This checks the request the provider sends, how streamed answers are parsed and
how errors are mapped. It does not verify the real API (see docs/llm-providers.md).
"""

import asyncio
import json
import logging
from collections.abc import Callable

import httpx
import pytest

from app.core.config import Settings
from app.llm import create_llm_provider
from app.llm.base import (
    GenerationDone,
    GenerationRequest,
    PromptMessage,
    ProviderError,
    ProviderErrorCode,
    StopReason,
    TextDelta,
)
from app.llm.catalog import AvailabilityState, Cost
from app.llm.gemini import GeminiBackend, GeminiProvider, create_client

API_KEY = "AIza-test-0123456789-do-not-log"
MODEL = "gemini-3.8-flash"

REQUEST = GenerationRequest(
    system="system rules",
    messages=(
        PromptMessage("user", "earlier question"),
        PromptMessage("assistant", "earlier answer"),
        PromptMessage("user", "<sources>…</sources> question"),
    ),
    max_output_tokens=512,
)

Handler = Callable[[httpx.Request], httpx.Response]


def chunk(
    *parts: dict,
    finish: str | None = None,
    usage: dict | None = None,
    model: str = MODEL,
    feedback: dict | None = None,
) -> dict:
    data: dict = {"modelVersion": model, "responseId": "resp-1"}
    if parts or finish:
        candidate: dict = {"content": {"role": "model", "parts": list(parts)}}
        if finish:
            candidate["finishReason"] = finish
        data["candidates"] = [candidate]
    if usage:
        data["usageMetadata"] = usage
    if feedback:
        data["promptFeedback"] = feedback
    return data


def sse(*chunks: dict) -> bytes:
    return "".join(f"data: {json.dumps(c)}\r\n\r\n" for c in chunks).encode()


def answer(*texts: str, finish: str = "STOP", usage: dict | None = None) -> bytes:
    chunks = [chunk({"text": t}) for t in texts[:-1]]
    chunks.append(
        chunk(
            {"text": texts[-1]},
            finish=finish,
            usage=usage
            or {
                "promptTokenCount": 300,
                "cachedContentTokenCount": 40,
                "candidatesTokenCount": 25,
                "thoughtsTokenCount": 15,
            },
        )
    )
    return sse(*chunks)


def ok(body: bytes) -> httpx.Response:
    return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


def error(status: int, grpc_status: str, *details: dict) -> httpx.Response:
    return httpx.Response(
        status,
        json={
            "error": {
                "code": status,
                "message": f"provider text with secret prompt content {API_KEY}",
                "status": grpc_status,
                "details": list(details),
            }
        },
    )


def _settings(make_settings: Callable[..., Settings], **overrides: object) -> Settings:
    return make_settings(gemini_api_key=API_KEY, **overrides)


def _provider(
    make_settings: Callable[..., Settings], handler: Handler, **overrides: object
) -> GeminiProvider:
    settings = _settings(make_settings, **overrides)
    client = create_client(
        settings,
        retry_initial_delay=0.01,
        base_url="http://gemini.test",
        httpx_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return GeminiProvider(settings, client, model=MODEL)


async def _collect(provider: GeminiProvider) -> tuple[str, GenerationDone]:
    text, done = [], None
    async for event in provider.stream(REQUEST):
        if isinstance(event, TextDelta):
            text.append(event.text)
        else:
            done = event
    assert done is not None
    return "".join(text), done


def _failure(provider: GeminiProvider) -> ProviderError:
    with pytest.raises(ProviderError) as info:
        asyncio.run(_collect(provider))
    return info.value


def test_streamed_answer_and_request_shape(make_settings: Callable[..., Settings]) -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return ok(
            sse(
                chunk({"text": "hidden reasoning", "thought": True}, {"text": "Remote work "}),
                chunk({"text": "is allowed [S1]."}),
                chunk(
                    {"text": ""},
                    finish="STOP",
                    usage={
                        "promptTokenCount": 300,
                        "cachedContentTokenCount": 40,
                        "candidatesTokenCount": 25,
                        "thoughtsTokenCount": 15,
                    },
                    model="gemini-3.8-flash-001",
                ),
            )
        )

    text, done = asyncio.run(_collect(_provider(make_settings, handler)))
    assert text == "Remote work is allowed [S1]."  # the thought part is dropped
    assert done.stop_reason is StopReason.END and done.raw_stop_reason == "STOP"
    assert done.model == "gemini-3.8-flash-001"  # the version that answered
    assert done.provider_request_id == "resp-1"
    # Cached tokens are reported apart; thinking tokens are billed output.
    assert (done.usage.input_tokens, done.usage.cache_read_input_tokens) == (260, 40)
    assert done.usage.output_tokens == 40

    (request,) = seen
    assert request.url.path == f"/v1beta/models/{MODEL}:streamGenerateContent"
    assert request.url.params["alt"] == "sse"
    assert request.headers["x-goog-api-key"] == API_KEY
    body = json.loads(request.content)
    assert body["systemInstruction"]["parts"] == [{"text": "system rules"}]
    assert [(c["role"], c["parts"][0]["text"]) for c in body["contents"]] == [
        ("user", "earlier question"),
        ("model", "earlier answer"),
        ("user", "<sources>…</sources> question"),
    ]
    config = body["generationConfig"]
    assert config["maxOutputTokens"] == 512 and config["candidateCount"] == 1
    # Unset by default: no temperature, no thinking configuration, no thought summaries.
    assert "temperature" not in config and "thinkingConfig" not in config


def test_optional_parameters(make_settings: Callable[..., Settings]) -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return ok(answer("ok"))

    provider = _provider(make_settings, handler, llm_temperature=0.2, gemini_thinking_level="low")
    asyncio.run(_collect(provider))
    config = seen[0]["generationConfig"]
    assert config["temperature"] == 0.2
    thinking = config["thinkingConfig"]
    assert thinking.get("thinkingLevel", thinking.get("thinking_level")) == "LOW"
    assert "includeThoughts" not in thinking and "include_thoughts" not in thinking


@pytest.mark.parametrize(
    ("finish", "stop"),
    [
        ("MAX_TOKENS", StopReason.MAX_TOKENS),
        ("SAFETY", StopReason.REFUSAL),
        ("RECITATION", StopReason.REFUSAL),
        ("PROHIBITED_CONTENT", StopReason.REFUSAL),
        ("OTHER", StopReason.OTHER),
    ],
)
def test_finish_reasons(
    make_settings: Callable[..., Settings], finish: str, stop: StopReason
) -> None:
    provider = _provider(make_settings, lambda r: ok(answer("Partial", finish=finish)))
    _, done = asyncio.run(_collect(provider))
    assert done.stop_reason is stop and done.raw_stop_reason == finish


def test_blocked_prompt_is_a_refusal(make_settings: Callable[..., Settings]) -> None:
    body = sse(
        chunk(
            feedback={"blockReason": "SAFETY"},
            usage={"promptTokenCount": 12, "candidatesTokenCount": 0},
        )
    )
    text, done = asyncio.run(_collect(_provider(make_settings, lambda r: ok(body))))
    assert text == ""
    assert done.stop_reason is StopReason.REFUSAL and done.raw_stop_reason == "PROMPT_BLOCKED"


def test_stream_without_finish_reason_is_invalid(make_settings: Callable[..., Settings]) -> None:
    body = sse(chunk({"text": "cut off"}))
    failure = _failure(_provider(make_settings, lambda r: ok(body)))
    assert failure.code is ProviderErrorCode.INVALID_RESPONSE and failure.retryable


RETRY_INFO = {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37s"}
KEY_INVALID = {
    "@type": "type.googleapis.com/google.rpc.ErrorInfo",
    "reason": "API_KEY_INVALID",
    "domain": "googleapis.com",
}


@pytest.mark.parametrize(
    ("response", "code", "retryable"),
    [
        (error(429, "RESOURCE_EXHAUSTED", RETRY_INFO), ProviderErrorCode.RATE_LIMITED, True),
        (error(400, "INVALID_ARGUMENT", KEY_INVALID), ProviderErrorCode.AUTHENTICATION, False),
        (error(403, "PERMISSION_DENIED"), ProviderErrorCode.PERMISSION, False),
        (error(400, "FAILED_PRECONDITION"), ProviderErrorCode.PERMISSION, False),
        (error(404, "NOT_FOUND"), ProviderErrorCode.MODEL_NOT_FOUND, False),
        (error(400, "INVALID_ARGUMENT"), ProviderErrorCode.BAD_REQUEST, False),
        (error(500, "INTERNAL"), ProviderErrorCode.SERVER_ERROR, True),
        (error(503, "UNAVAILABLE"), ProviderErrorCode.OVERLOADED, True),
        (error(504, "DEADLINE_EXCEEDED"), ProviderErrorCode.TIMEOUT, True),
    ],
)
def test_status_errors_are_mapped_without_provider_text(
    make_settings: Callable[..., Settings],
    caplog: pytest.LogCaptureFixture,
    response: httpx.Response,
    code: ProviderErrorCode,
    retryable: bool,
) -> None:
    caplog.set_level(logging.DEBUG)
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return response

    failure = _failure(_provider(make_settings, handler, llm_max_retries=0))
    assert failure.code is code and failure.retryable is retryable
    assert calls == 1  # LLM_MAX_RETRIES=0: no retry
    assert "provider text" not in failure.message and API_KEY not in failure.message
    assert API_KEY not in caplog.text and "secret prompt content" not in caplog.text
    if code is ProviderErrorCode.RATE_LIMITED:
        assert failure.retry_after_seconds == 37.0


def test_rate_limit_is_retried_before_streaming(make_settings: Callable[..., Settings]) -> None:
    responses = [error(429, "RESOURCE_EXHAUSTED"), error(503, "UNAVAILABLE"), ok(answer("ok"))]

    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    text, _ = asyncio.run(_collect(_provider(make_settings, handler, llm_max_retries=2)))
    assert text == "ok" and responses == []


def test_non_retryable_errors_are_not_retried(make_settings: Callable[..., Settings]) -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return error(404, "NOT_FOUND")

    _failure(_provider(make_settings, handler, llm_max_retries=3))
    assert calls == 1


def test_mid_stream_error(make_settings: Callable[..., Settings]) -> None:
    body = sse(chunk({"text": "Remote work "})) + (
        b'data: {"error": {"code": 500, "message": "boom", "status": "INTERNAL"}}\r\n\r\n'
    )
    seen: list[str] = []

    async def run() -> None:
        async for event in _provider(make_settings, lambda r: ok(body)).stream(REQUEST):
            if isinstance(event, TextDelta):
                seen.append(event.text)

    with pytest.raises(ProviderError) as info:
        asyncio.run(run())
    assert seen == ["Remote work "]
    assert info.value.code is ProviderErrorCode.SERVER_ERROR


@pytest.mark.parametrize(
    ("exception", "code"),
    [
        (httpx.ReadTimeout("slow"), ProviderErrorCode.TIMEOUT),
        (httpx.ConnectError("refused"), ProviderErrorCode.CONNECTION),
    ],
)
def test_transport_errors(
    make_settings: Callable[..., Settings], exception: Exception, code: ProviderErrorCode
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exception

    failure = _failure(_provider(make_settings, handler, llm_max_retries=0))
    assert failure.code is code and failure.retryable


def test_closing_the_stream_early_stops_reading(make_settings: Callable[..., Settings]) -> None:
    state = {"sent": 0, "closed": False}

    class Body(httpx.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            for i in range(100):
                state["sent"] += 1
                yield sse(chunk({"text": f"part {i} "}))
                await asyncio.sleep(0)

        async def aclose(self) -> None:
            state["closed"] = True

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Body(), headers={"content-type": "text/event-stream"})

    async def run() -> None:
        stream = _provider(make_settings, handler).stream(REQUEST)
        first = await anext(stream)
        assert isinstance(first, TextDelta)
        await stream.aclose()  # type: ignore[attr-defined]
        for _ in range(5):
            await asyncio.sleep(0)

    asyncio.run(run())
    assert state["closed"] is True
    assert state["sent"] < 100


def test_api_key_never_logged(
    make_settings: Callable[..., Settings], caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    asyncio.run(_collect(_provider(make_settings, lambda r: ok(answer("ok")))))
    assert API_KEY not in caplog.text


def test_default_provider_without_key(make_settings: Callable[..., Settings]) -> None:
    assert create_llm_provider(make_settings(llm_provider="gemini")) is None
    provider = create_llm_provider(make_settings(llm_provider="gemini", gemini_api_key=API_KEY))
    assert isinstance(provider, GeminiProvider) and provider.model == MODEL


def _backend(make_settings: Callable[..., Settings], handler: Handler, **overrides: object):  # type: ignore[no-untyped-def]
    settings = _settings(
        make_settings, gemini_models="gemini-3.8-flash,gemini-3.5-flash-lite,gemini-9", **overrides
    )
    client = create_client(
        settings,
        base_url="http://gemini.test",
        httpx_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    return GeminiBackend(settings, client)


def test_backend_checks_each_model(make_settings: Callable[..., Settings]) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.path.rsplit("/", 1)[-1]
        if name == "gemini-9":
            return error(404, "NOT_FOUND")
        if name == "gemini-3.5-flash-lite":
            raise httpx.ConnectError("refused")
        return httpx.Response(200, json={"name": f"models/{name}", "displayName": "x"})

    backend = _backend(make_settings, handler)
    results = asyncio.run(backend.check())
    assert results["gemini-3.8-flash"].state is AvailabilityState.AVAILABLE
    assert results["gemini-9"].state is AvailabilityState.UNAVAILABLE
    assert results["gemini-3.5-flash-lite"].state is AvailabilityState.UNKNOWN
    assert backend.cost is Cost.FREE_TIER and "improve its products" in (backend.notice or "")


def test_backend_rejected_key(make_settings: Callable[..., Settings]) -> None:
    backend = _backend(
        make_settings, lambda r: error(400, "INVALID_ARGUMENT", KEY_INVALID), gemini_tier="paid"
    )
    results = asyncio.run(backend.check())
    assert {r.state for r in results.values()} == {AvailabilityState.UNAVAILABLE}
    assert all(API_KEY not in (r.reason or "") for r in results.values())
    assert backend.cost is Cost.PAID


def test_backend_without_key(make_settings: Callable[..., Settings]) -> None:
    backend = GeminiBackend(make_settings())
    assert not backend.configured
    assert backend.not_configured_reason == "GEMINI_API_KEY is not set"
