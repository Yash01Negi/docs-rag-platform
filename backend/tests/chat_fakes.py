"""A scripted LLM provider for tests. It never runs outside the test suite.

Each script item is either text to stream, a ``ProviderError`` (or any exception) to raise,
a float (seconds to sleep), or an ``asyncio.Event``-like gate to wait for. A script can
also be a function of the request, so a test can answer from the sources it was given.
"""

import asyncio
import re
import threading
from collections.abc import AsyncIterator, Callable, Sequence
from dataclasses import dataclass

from app.llm.base import (
    GenerationDone,
    GenerationEvent,
    GenerationRequest,
    LLMProvider,
    StopReason,
    TextDelta,
    Usage,
)

ScriptItem = "str | BaseException | float | Gate"
Script = Sequence[ScriptItem] | Callable[[GenerationRequest], Sequence[ScriptItem]]


@dataclass
class Gate:
    """A thread-safe gate a script waits on (tests release it from the test thread)."""

    event: threading.Event

    @classmethod
    def closed(cls) -> "Gate":
        return cls(threading.Event())


DEFAULT_USAGE = Usage(input_tokens=120, output_tokens=30)


class ScriptedProvider(LLMProvider):
    name = "test-double"

    def __init__(
        self,
        script: Script = ("A grounded answer [S1].",),
        *,
        model: str = "scripted-model",
        served_model: str | None = None,
        stop_reason: StopReason = StopReason.END,
        usage: Usage = DEFAULT_USAGE,
    ) -> None:
        self.model = model
        self.script = script
        self.served_model = served_model or model
        self.stop_reason = stop_reason
        self.usage = usage
        self.requests: list[GenerationRequest] = []
        self.started = threading.Event()
        self.closed_early = 0
        self.completed = 0

    async def stream(self, request: GenerationRequest) -> AsyncIterator[GenerationEvent]:
        self.requests.append(request)
        self.started.set()
        items = self.script(request) if callable(self.script) else self.script
        finished = False
        try:
            for item in items:
                if isinstance(item, BaseException):
                    raise item
                if isinstance(item, float):
                    await asyncio.sleep(item)
                elif isinstance(item, Gate):
                    while not item.event.is_set():
                        await asyncio.sleep(0.01)
                else:
                    yield TextDelta(item)
            finished = True
            self.completed += 1
            yield GenerationDone(
                stop_reason=self.stop_reason,
                usage=self.usage,
                model=self.served_model,
                provider_request_id="req_test",
                raw_stop_reason={StopReason.END: "end_turn"}.get(self.stop_reason),
            )
        finally:
            if not finished:
                self.closed_early += 1


_SOURCE = re.compile(r'<source id="S(\d+)"[^>]*>\n(.*?)\n</source>', re.DOTALL)


def sources_in(request: GenerationRequest) -> dict[int, str]:
    """The numbered source texts in the request's last user message."""
    return {int(n): text for n, text in _SOURCE.findall(request.messages[-1].content)}


def cite_first_source(request: GenerationRequest) -> list[str]:
    """Answer with the first source's text, cited, in two deltas (a deterministic stand-in)."""
    sources = sources_in(request)
    if not sources:
        return ["[NOT_FOUND] The documents do not say."]
    text = sources[1].split("\n")[0][:200]
    return [f"The documents say: {text} [S", "1]."]
