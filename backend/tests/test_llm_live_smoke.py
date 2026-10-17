"""Opt-in smoke tests against the real language model providers.

Skipped unless RUN_LLM_SMOKE_TESTS=1. Each provider runs only when it is configured through
the usual environment variables (ANTHROPIC_API_KEY, GEMINI_API_KEY, OLLAMA_BASE_URL and
OLLAMA_MODELS); the first model in each provider's list is used. Every run sends a few short
requests and is billed (or counted against free-tier limits) like any other request:

    RUN_LLM_SMOKE_TESTS=1 GEMINI_API_KEY=... uv run pytest -m live_llm -s

Never run in normal CI. See docs/llm-smoke-tests.md.
"""

import asyncio
import os

import pytest

from app.core.config import Settings
from app.llm import create_llm_registry
from app.llm.base import GenerationDone, GenerationRequest, PromptMessage, StopReason, TextDelta
from app.llm.catalog import AvailabilityState

pytestmark = pytest.mark.live_llm

if os.environ.get("RUN_LLM_SMOKE_TESTS") != "1":
    pytest.skip("set RUN_LLM_SMOKE_TESTS=1 to call the real LLM providers", allow_module_level=True)

_REQUEST = GenerationRequest(
    system="You are a test endpoint. Follow the instruction exactly.",
    messages=(PromptMessage("user", "Reply with the single word: pong"),),
    max_output_tokens=256,
)


def _settings() -> Settings:
    # Provider settings come from the environment; the database is not used.
    return Settings(database_url="postgresql+psycopg://unused:unused@127.0.0.1:1/unused")  # type: ignore[call-arg]


def _configured(name: str) -> bool:
    backend = create_llm_registry(_settings()).backends[name]
    return backend.configured


@pytest.mark.parametrize("name", ["anthropic", "gemini", "ollama"])
def test_provider_answers_and_reports_usage(name: str) -> None:
    if not _configured(name):
        pytest.skip(f"{name} is not configured in the environment")

    async def run() -> None:
        registry = create_llm_registry(_settings())
        backend = registry.backends[name]
        try:
            model = backend.models[0]
            availability = (await backend.check()).get(model)
            assert availability is not None
            assert availability.state is AvailabilityState.AVAILABLE, availability.reason
            text, done = "", None
            async for event in backend.provider(model).stream(_REQUEST):
                if isinstance(event, TextDelta):
                    text += event.text
                elif isinstance(event, GenerationDone):
                    done = event
            assert done is not None
            assert done.stop_reason is StopReason.END, done.raw_stop_reason
            assert "pong" in text.lower()
            assert done.usage.input_tokens > 0 and done.usage.output_tokens > 0
            print(f"\n{name}: served by {done.model}, usage {done.usage}, text {text!r}")
        finally:
            await registry.aclose()

    asyncio.run(run())


@pytest.mark.parametrize("name", ["anthropic", "gemini", "ollama"])
def test_provider_stream_can_be_stopped(name: str) -> None:
    if not _configured(name):
        pytest.skip(f"{name} is not configured in the environment")
    request = GenerationRequest(
        system=_REQUEST.system,
        messages=(PromptMessage("user", "Count from 1 to 200, one number per line."),),
        max_output_tokens=1024,
    )

    async def run() -> None:
        registry = create_llm_registry(_settings())
        backend = registry.backends[name]
        try:
            stream = backend.provider(backend.models[0]).stream(request)
            async for event in stream:
                if isinstance(event, TextDelta):
                    break
            await stream.aclose()  # type: ignore[attr-defined]
        finally:
            await registry.aclose()

    asyncio.run(asyncio.wait_for(run(), timeout=120))
