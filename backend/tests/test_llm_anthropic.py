"""Contract tests for the Anthropic provider, using the real SDK against a mock transport.

No request leaves the process: an ``httpx2.MockTransport`` answers with recorded-shape
Messages API responses (streamed events and error bodies). This checks the request the
provider sends, how it parses streamed answers, and how SDK errors are mapped. It does not
verify behaviour of the real API (see docs/llm-providers.md, "Verification status").
"""

import asyncio
import json
import logging
from collections.abc import Callable

import anthropic
import httpx2
import pytest

from app.core.config import Settings
from app.llm import create_llm_provider
from app.llm.anthropic import FALLBACK_BETA, AnthropicProvider, map_error
from app.llm.base import (
    GenerationDone,
    GenerationRequest,
    PromptMessage,
    ProviderError,
    ProviderErrorCode,
    StopReason,
    TextDelta,
)

API_KEY = "sk-ant-test-0123456789-do-not-log"

REQUEST = GenerationRequest(
    system="system rules",
    messages=(
        PromptMessage("user", "earlier question"),
        PromptMessage("assistant", "earlier answer"),
        PromptMessage("user", "<sources>…</sources> question"),
    ),
    max_output_tokens=512,
)


def _sse(events: list[tuple[str, dict]]) -> bytes:
    return "".join(f"event: {name}\ndata: {json.dumps(data)}\n\n" for name, data in events).encode()


def _stream_body(
    deltas: list[str],
    *,
    model: str = "claude-opus-5-5",
    stop_reason: str = "end_turn",
    usage: dict | None = None,
    thinking: bool = True,
) -> bytes:
    events: list[tuple[str, dict]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 321, "output_tokens": 1},
                },
            },
        )
    ]
    index = 0
    if thinking:
        events += [
            (
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "thinking", "thinking": "", "signature": ""},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "hidden reasoning"},
                },
            ),
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "signature_delta", "signature": "sig"},
                },
            ),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        ]
        index = 1
    events.append(
        (
            "content_block_start",
            {
                "type": "content_block_start",
                "index": index,
                "content_block": {"type": "text", "text": ""},
            },
        )
    )
    for text in deltas:
        events.append(
            (
                "content_block_delta",
                {
                    "type": "content_block_delta",
                    "index": index,
                    "delta": {"type": "text_delta", "text": text},
                },
            )
        )
    events += [
        ("content_block_stop", {"type": "content_block_stop", "index": index}),
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": usage or {"output_tokens": 42, "cache_read_input_tokens": 7},
            },
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    return _sse(events)


def _client(handler: Callable[[httpx2.Request], httpx2.Response]) -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(
        api_key=API_KEY,
        base_url="http://anthropic.test",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )


def _settings(make_settings: Callable[..., Settings], **overrides: object) -> Settings:
    return make_settings(anthropic_api_key=API_KEY, **overrides)


async def _collect(provider: AnthropicProvider) -> tuple[str, GenerationDone]:
    text, done = [], None
    async for event in provider.stream(REQUEST):
        if isinstance(event, TextDelta):
            text.append(event.text)
        else:
            done = event
    assert done is not None
    return "".join(text), done


def test_streamed_answer_and_request_shape(make_settings: Callable[..., Settings]) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream", "request-id": "req_abc"},
            content=_stream_body(["Remote work ", "is allowed [S1]."]),
        )

    provider = AnthropicProvider(_settings(make_settings), client=_client(handler))
    text, done = asyncio.run(_collect(provider))

    assert text == "Remote work is allowed [S1]."  # thinking is never surfaced
    assert done.stop_reason is StopReason.END
    assert done.raw_stop_reason == "end_turn"
    assert done.model == "claude-opus-5-5"
    assert done.provider_request_id == "req_abc"
    assert (done.usage.input_tokens, done.usage.output_tokens) == (321, 42)
    assert done.usage.cache_read_input_tokens == 7

    request = seen[0]
    assert request.url.path == "/v1/messages"
    assert request.headers["x-api-key"] == API_KEY
    assert FALLBACK_BETA in request.headers["anthropic-beta"]
    body = json.loads(request.content)
    assert body["model"] == "claude-opus-5-5"
    assert body["max_tokens"] == 512
    assert body["stream"] is True
    assert body["system"] == "system rules"
    assert [m["role"] for m in body["messages"]] == ["user", "assistant", "user"]
    assert body["fallbacks"] == "default"
    # Current models reject sampling parameters and disabled thinking; none are sent.
    assert "temperature" not in body
    assert "thinking" not in body
    assert "output_config" not in body


def test_optional_parameters(make_settings: Callable[..., Settings]) -> None:
    seen: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=_stream_body(["ok"])
        )

    settings = _settings(
        make_settings,
        llm_model="claude-sonnet-5-5",
        llm_temperature=0.2,
        llm_effort="low",
        llm_fallbacks="none",
    )
    provider = AnthropicProvider(settings, client=_client(handler))
    asyncio.run(_collect(provider))
    body = seen[0]
    assert body["model"] == "claude-sonnet-5-5"
    assert body["temperature"] == 0.2
    assert body["output_config"] == {"effort": "low"}
    assert "fallbacks" not in body


def test_fallback_model_and_refusal_are_reported(make_settings: Callable[..., Settings]) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_stream_body([], model="claude-opus-4-8", stop_reason="refusal"),
        )

    provider = AnthropicProvider(_settings(make_settings), client=_client(handler))
    text, done = asyncio.run(_collect(provider))
    assert text == ""
    assert done.stop_reason is StopReason.REFUSAL
    assert done.model == "claude-opus-4-8"


def test_max_tokens_stop(make_settings: Callable[..., Settings]) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_stream_body(["cut"], stop_reason="max_tokens"),
        )

    provider = AnthropicProvider(_settings(make_settings), client=_client(handler))
    assert asyncio.run(_collect(provider))[1].stop_reason is StopReason.MAX_TOKENS


def _error(status: int, error_type: str, headers: dict | None = None) -> httpx2.Response:
    return httpx2.Response(
        status,
        headers={"request-id": "req_err", **(headers or {})},
        json={
            "type": "error",
            "error": {"type": error_type, "message": "raw provider text: echo of the prompt"},
        },
    )


@pytest.mark.parametrize(
    ("status", "error_type", "code", "retryable"),
    [
        (429, "rate_limit_error", ProviderErrorCode.RATE_LIMITED, True),
        (529, "overloaded_error", ProviderErrorCode.OVERLOADED, True),
        (500, "api_error", ProviderErrorCode.SERVER_ERROR, True),
        (401, "authentication_error", ProviderErrorCode.AUTHENTICATION, False),
        (403, "permission_error", ProviderErrorCode.PERMISSION, False),
        (402, "billing_error", ProviderErrorCode.PERMISSION, False),
        (404, "not_found_error", ProviderErrorCode.MODEL_NOT_FOUND, False),
        (413, "request_too_large", ProviderErrorCode.REQUEST_TOO_LARGE, False),
        (400, "invalid_request_error", ProviderErrorCode.BAD_REQUEST, False),
    ],
)
def test_http_errors_are_mapped(
    make_settings: Callable[..., Settings],
    status: int,
    error_type: str,
    code: ProviderErrorCode,
    retryable: bool,
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return _error(status, error_type, {"retry-after": "17"})

    provider = AnthropicProvider(_settings(make_settings), client=_client(handler))
    with pytest.raises(ProviderError) as caught:
        asyncio.run(_collect(provider))
    error = caught.value
    assert error.code is code
    assert error.retryable is retryable
    assert error.status_code == status
    assert error.provider_request_id == "req_err"
    assert "raw provider text" not in error.message
    if status == 429:
        assert error.retry_after_seconds == 17


def test_error_event_in_the_middle_of_a_stream(make_settings: Callable[..., Settings]) -> None:
    body = _stream_body(["partial "], thinking=False).split(b"event: content_block_stop")[0] + _sse(
        [("error", {"type": "error", "error": {"type": "overloaded_error", "message": "x"}})]
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)

    provider = AnthropicProvider(_settings(make_settings), client=_client(handler))
    received: list[str] = []

    async def run() -> None:
        async for event in provider.stream(REQUEST):
            if isinstance(event, TextDelta):
                received.append(event.text)

    with pytest.raises(ProviderError) as caught:
        asyncio.run(run())
    assert received == ["partial "]
    assert caught.value.code is ProviderErrorCode.OVERLOADED


def test_connection_failures_and_timeouts(make_settings: Callable[..., Settings]) -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused")

    def slow(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("timed out")

    for handler, code in (
        (refuse, ProviderErrorCode.CONNECTION),
        (slow, ProviderErrorCode.TIMEOUT),
    ):
        provider = AnthropicProvider(_settings(make_settings), client=_client(handler))
        with pytest.raises(ProviderError) as caught:
            asyncio.run(_collect(provider))
        assert caught.value.code is code
        assert caught.value.retryable


def test_sdk_retries_rate_limits_before_streaming(make_settings: Callable[..., Settings]) -> None:
    calls: list[int] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        calls.append(1)
        if len(calls) == 1:
            return _error(429, "rate_limit_error", {"retry-after": "0"})
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=_stream_body(["ok"])
        )

    client = anthropic.AsyncAnthropic(
        api_key=API_KEY,
        base_url="http://anthropic.test",
        max_retries=2,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )
    provider = AnthropicProvider(_settings(make_settings), client=client)
    assert asyncio.run(_collect(provider))[0] == "ok"
    assert len(calls) == 2


def test_closing_the_stream_early_stops_reading(make_settings: Callable[..., Settings]) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=_stream_body(["one ", "two ", "three"]),
        )

    provider = AnthropicProvider(_settings(make_settings), client=_client(handler))

    async def first_only() -> str:
        stream = provider.stream(REQUEST)
        first = await anext(stream)
        await stream.aclose()  # type: ignore[attr-defined]
        assert isinstance(first, TextDelta)
        return first.text

    assert asyncio.run(first_only()) == "one "


def test_generate_collects_a_complete_answer(make_settings: Callable[..., Settings]) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200, headers={"content-type": "text/event-stream"}, content=_stream_body(["a", "b"])
        )

    provider = AnthropicProvider(_settings(make_settings), client=_client(handler))
    result = asyncio.run(provider.generate(REQUEST))
    assert result.text == "ab"
    assert result.done.usage.output_tokens == 42


def test_api_key_is_never_logged(
    make_settings: Callable[..., Settings], caplog: pytest.LogCaptureFixture
) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return _error(401, "authentication_error")

    caplog.set_level(logging.DEBUG)
    settings = _settings(make_settings)
    provider = AnthropicProvider(settings, client=_client(handler))
    with pytest.raises(ProviderError) as caught:
        asyncio.run(_collect(provider))
    assert API_KEY not in caplog.text
    assert API_KEY not in repr(caught.value)
    assert API_KEY not in repr(settings)
    assert API_KEY not in settings.model_dump_json()


def test_unexpected_exceptions_map_to_invalid_response() -> None:
    error = map_error(RuntimeError("boom"))
    assert error.code is ProviderErrorCode.INVALID_RESPONSE
    assert "boom" not in error.message


def test_factory_requires_a_key(make_settings: Callable[..., Settings]) -> None:
    assert create_llm_provider(make_settings()) is None
    assert create_llm_provider(make_settings(anthropic_api_key="   ")) is None
    provider = create_llm_provider(make_settings(anthropic_api_key=API_KEY))
    assert isinstance(provider, AnthropicProvider)
    assert provider.model == "claude-opus-5-5"
    asyncio.run(provider.aclose())
