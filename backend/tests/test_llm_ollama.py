"""Contract tests for the Ollama provider against a mock HTTP transport.

An ``httpx2.MockTransport`` answers like Ollama's ``/api/chat`` (newline-delimited JSON)
and ``/api/tags``. No Ollama server is needed or contacted; real local inference is not
verified by these tests (see docs/llm-providers.md).
"""

import asyncio
import json
import logging
from collections.abc import Callable

import httpx2
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
from app.llm.ollama import OllamaBackend, OllamaProvider

BASE_URL = "http://ollama.test:11434"
MODEL = "qwen3:8b"

REQUEST = GenerationRequest(
    system="system rules",
    messages=(
        PromptMessage("user", "earlier question"),
        PromptMessage("assistant", "earlier answer"),
        PromptMessage("user", "<sources>…</sources> question"),
    ),
    max_output_tokens=512,
)

Handler = Callable[[httpx2.Request], httpx2.Response]


def ndjson(*lines: dict) -> bytes:
    return "".join(json.dumps(line) + "\n" for line in lines).encode()


def answer(*texts: str, done_reason: str = "stop") -> bytes:
    lines: list[dict] = [
        {"model": MODEL, "message": {"role": "assistant", "content": t}, "done": False}
        for t in texts
    ]
    lines.append(
        {
            "model": MODEL,
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "done_reason": done_reason,
            "prompt_eval_count": 812,
            "eval_count": 64,
        }
    )
    return ndjson(*lines)


def ok(body: bytes) -> httpx2.Response:
    return httpx2.Response(200, content=body, headers={"content-type": "application/x-ndjson"})


def _settings(make_settings: Callable[..., Settings], **overrides: object) -> Settings:
    values: dict[str, object] = {"ollama_base_url": BASE_URL, "ollama_models": MODEL}
    values.update(overrides)
    return make_settings(**values)


def _client(handler: Handler) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(base_url=BASE_URL, transport=httpx2.MockTransport(handler))


def _provider(
    make_settings: Callable[..., Settings], handler: Handler, **overrides: object
) -> tuple[OllamaProvider, list[float]]:
    sleeps: list[float] = []

    async def sleep(seconds: float) -> None:
        sleeps.append(seconds)

    provider = OllamaProvider(
        _settings(make_settings, **overrides), _client(handler), model=MODEL, sleep=sleep
    )
    return provider, sleeps


async def _collect(provider: OllamaProvider) -> tuple[str, GenerationDone]:
    text, done = [], None
    async for event in provider.stream(REQUEST):
        if isinstance(event, TextDelta):
            text.append(event.text)
        else:
            done = event
    assert done is not None
    return "".join(text), done


def _failure(provider: OllamaProvider) -> ProviderError:
    with pytest.raises(ProviderError) as info:
        asyncio.run(_collect(provider))
    return info.value


def test_streamed_answer_and_request_shape(make_settings: Callable[..., Settings]) -> None:
    seen: list[httpx2.Request] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return ok(
            ndjson(
                {
                    "model": MODEL,
                    "message": {"role": "assistant", "content": "", "thinking": "hmm"},
                    "done": False,
                },
            )
            + answer("Remote work ", "is allowed [S1].")
        )

    provider, _ = _provider(make_settings, handler)
    text, done = asyncio.run(_collect(provider))
    assert text == "Remote work is allowed [S1]."  # thinking is dropped
    assert done.stop_reason is StopReason.END and done.raw_stop_reason == "stop"
    assert (done.usage.input_tokens, done.usage.output_tokens) == (812, 64)
    assert done.model == MODEL

    (request,) = seen
    assert request.method == "POST" and str(request.url) == f"{BASE_URL}/api/chat"
    body = json.loads(request.content)
    assert body["model"] == MODEL and body["stream"] is True
    assert body["messages"] == [
        {"role": "system", "content": "system rules"},
        {"role": "user", "content": "earlier question"},
        {"role": "assistant", "content": "earlier answer"},
        {"role": "user", "content": "<sources>…</sources> question"},
    ]
    # The context window is always set: Ollama's default would cut the prompt short.
    assert body["options"] == {"num_predict": 512, "num_ctx": 16384}
    assert "think" not in body and "keep_alive" not in body


def test_optional_parameters(make_settings: Callable[..., Settings]) -> None:
    seen: list[dict] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        seen.append(json.loads(request.content))
        return ok(answer("ok"))

    provider, _ = _provider(
        make_settings,
        handler,
        llm_temperature=0.1,
        ollama_think=False,
        ollama_keep_alive="10m",
        ollama_num_ctx=32768,
    )
    asyncio.run(_collect(provider))
    body = seen[0]
    assert body["options"] == {"num_predict": 512, "num_ctx": 32768, "temperature": 0.1}
    assert body["think"] is False and body["keep_alive"] == "10m"


def test_length_stop(make_settings: Callable[..., Settings]) -> None:
    provider, _ = _provider(make_settings, lambda r: ok(answer("cut", done_reason="length")))
    _, done = asyncio.run(_collect(provider))
    assert done.stop_reason is StopReason.MAX_TOKENS


@pytest.mark.parametrize(
    ("status", "code", "retryable"),
    [
        (404, ProviderErrorCode.MODEL_NOT_FOUND, False),
        (400, ProviderErrorCode.BAD_REQUEST, False),
        (403, ProviderErrorCode.PERMISSION, False),
        (429, ProviderErrorCode.RATE_LIMITED, True),
        (500, ProviderErrorCode.SERVER_ERROR, True),
        (503, ProviderErrorCode.OVERLOADED, True),
    ],
)
def test_status_errors(
    make_settings: Callable[..., Settings],
    caplog: pytest.LogCaptureFixture,
    status: int,
    code: ProviderErrorCode,
    retryable: bool,
) -> None:
    caplog.set_level(logging.DEBUG)

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(status, json={"error": "model text with secret prompt content"})

    provider, _ = _provider(make_settings, handler, llm_max_retries=0)
    failure = _failure(provider)
    assert failure.code is code and failure.retryable is retryable
    assert "secret prompt content" not in failure.message
    assert "secret prompt content" not in caplog.text


def test_transient_errors_are_retried_with_backoff(make_settings: Callable[..., Settings]) -> None:
    responses: list[object] = [
        httpx2.ConnectError("refused"),
        httpx2.Response(503, json={"error": "loading"}),
        httpx2.Response(429, headers={"retry-after": "3"}, json={"error": "busy"}),
        ok(answer("ok")),
    ]

    def handler(request: httpx2.Request) -> httpx2.Response:
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item  # type: ignore[return-value]

    provider, sleeps = _provider(make_settings, handler, llm_max_retries=3)
    text, _ = asyncio.run(_collect(provider))
    assert text == "ok" and responses == []
    assert sleeps == [0.5, 1.0, 3.0]  # exponential backoff, then Retry-After


def test_retries_are_bounded(make_settings: Callable[..., Settings]) -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        raise httpx2.ConnectError("refused")

    provider, sleeps = _provider(make_settings, handler, llm_max_retries=2)
    failure = _failure(provider)
    assert calls == 3 and len(sleeps) == 2
    assert failure.code is ProviderErrorCode.CONNECTION
    assert "host.docker.internal" in failure.message  # tells macOS Docker users what to set
    assert BASE_URL not in failure.message


def test_not_found_is_not_retried(make_settings: Callable[..., Settings]) -> None:
    calls = 0

    def handler(request: httpx2.Request) -> httpx2.Response:
        nonlocal calls
        calls += 1
        return httpx2.Response(404, json={"error": f"model '{MODEL}' not found"})

    provider, _ = _provider(make_settings, handler, llm_max_retries=3)
    assert _failure(provider).code is ProviderErrorCode.MODEL_NOT_FOUND
    assert calls == 1


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (
            ndjson({"model": MODEL, "message": {"content": "Partial "}, "done": False})
            + b'{"error":"model crashed"}\n',
            ProviderErrorCode.SERVER_ERROR,
        ),
        (b"not json\n", ProviderErrorCode.INVALID_RESPONSE),
        (b'["list"]\n', ProviderErrorCode.INVALID_RESPONSE),
        (
            ndjson({"model": MODEL, "message": {"content": "no end"}, "done": False}),
            ProviderErrorCode.INVALID_RESPONSE,
        ),
    ],
)
def test_bad_streams(
    make_settings: Callable[..., Settings], body: bytes, code: ProviderErrorCode
) -> None:
    provider, _ = _provider(make_settings, lambda r: ok(body), llm_max_retries=0)
    assert _failure(provider).code is code


def test_timeout(make_settings: Callable[..., Settings]) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ReadTimeout("slow")

    provider, _ = _provider(make_settings, handler, llm_max_retries=0)
    failure = _failure(provider)
    assert failure.code is ProviderErrorCode.TIMEOUT and failure.retryable


def test_closing_the_stream_early_stops_reading(make_settings: Callable[..., Settings]) -> None:
    state = {"sent": 0, "closed": False}

    class Body(httpx2.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            for i in range(100):
                state["sent"] += 1
                yield ndjson({"model": MODEL, "message": {"content": f"part {i} "}, "done": False})
                await asyncio.sleep(0)

        async def aclose(self) -> None:
            state["closed"] = True

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, stream=Body())

    async def run() -> None:
        provider, _ = _provider(make_settings, handler)
        stream = provider.stream(REQUEST)
        assert isinstance(await anext(stream), TextDelta)
        await stream.aclose()  # type: ignore[attr-defined]

    asyncio.run(run())
    assert state["closed"] is True and state["sent"] < 100


def test_default_provider(make_settings: Callable[..., Settings]) -> None:
    assert create_llm_provider(make_settings(llm_provider="ollama")) is None
    assert (
        create_llm_provider(make_settings(llm_provider="ollama", ollama_base_url=BASE_URL)) is None
    )  # no models allowed
    provider = create_llm_provider(_settings(make_settings, llm_provider="ollama"))
    assert isinstance(provider, OllamaProvider) and provider.model == MODEL


def _tags(*entries: dict) -> Handler:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.path == "/api/tags"
        return httpx2.Response(200, json={"models": list(entries)})

    return handler


def test_backend_compares_allowlist_with_installed_models(
    make_settings: Callable[..., Settings],
) -> None:
    settings = _settings(
        make_settings, ollama_models="qwen3:8b,gemma3,llama9:70b,gpt-oss:120b-cloud"
    )
    handler = _tags(
        {"name": "qwen3:8b", "model": "qwen3:8b", "size": 5_000_000_000},
        {"name": "gemma3:latest", "model": "gemma3:latest"},
        {
            "name": "gpt-oss:120b-cloud",
            "remote_model": "gpt-oss:120b",
            "remote_host": "https://ollama.com:443",
        },
        {"name": "unlisted:1b"},
    )
    backend = OllamaBackend(settings, _client(handler))
    results = asyncio.run(backend.check())
    assert results["qwen3:8b"].state is AvailabilityState.AVAILABLE
    assert results["gemma3"].state is AvailabilityState.AVAILABLE  # matches gemma3:latest
    assert results["llama9:70b"].state is AvailabilityState.UNAVAILABLE
    assert "ollama pull llama9:70b" in (results["llama9:70b"].reason or "")
    remote = results["gpt-oss:120b-cloud"]
    assert remote.state is AvailabilityState.UNAVAILABLE and "ollama.com" in (remote.reason or "")
    assert "unlisted:1b" not in results  # installed but not allowed: never offered
    assert backend.cost is Cost.LOCAL


def test_backend_allows_remote_models_when_configured(
    make_settings: Callable[..., Settings],
) -> None:
    settings = _settings(
        make_settings, ollama_models="gpt-oss:120b-cloud", ollama_allow_remote_models=True
    )
    handler = _tags({"name": "gpt-oss:120b-cloud", "remote_host": "https://ollama.com:443"})
    results = asyncio.run(OllamaBackend(settings, _client(handler)).check())
    assert results["gpt-oss:120b-cloud"].state is AvailabilityState.AVAILABLE


def test_backend_unreachable(make_settings: Callable[..., Settings]) -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("refused")

    results = asyncio.run(OllamaBackend(_settings(make_settings), _client(handler)).check())
    (result,) = results.values()
    assert result.state is AvailabilityState.UNAVAILABLE and result.transient
    assert "not running" in (result.reason or "") and BASE_URL not in (result.reason or "")


def test_backend_not_configured(make_settings: Callable[..., Settings]) -> None:
    assert OllamaBackend(make_settings()).not_configured_reason == "OLLAMA_BASE_URL is not set"
    backend = OllamaBackend(make_settings(ollama_base_url=BASE_URL))
    assert not backend.configured and backend.not_configured_reason == "OLLAMA_MODELS is empty"


@pytest.mark.parametrize(
    "url",
    [
        "ftp://ollama.test",
        "http://user:pw@ollama.test:11434",
        "http://ollama.test:11434/?x=1",
        "not a url",
    ],
)
def test_invalid_base_urls_are_rejected(make_settings: Callable[..., Settings], url: str) -> None:
    with pytest.raises(ValueError):
        make_settings(ollama_base_url=url)


def test_base_url_is_normalised(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings(ollama_base_url=" http://host.docker.internal:11434/ ")
    assert settings.ollama_base_url == "http://host.docker.internal:11434"
