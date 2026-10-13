"""Choosing a provider and model per question (Phase 4.1), against a real database.

Two kinds of providers are used, neither of which contacts a real model:

* ``FakeBackend`` with ``ScriptedProvider`` test doubles, for the selection rules, storage
  and model switching inside one conversation;
* the real Anthropic, Gemini and Ollama providers driven through their real SDK or HTTP
  client against mock transports, so the whole path (request, streamed response,
  citations, storage) is exercised for each provider's wire format.
"""

import asyncio
import json
import threading
import uuid
from collections.abc import Callable

import anthropic
import httpx
import httpx2
import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from app.chat.prompt import SYSTEM_PROMPT
from app.core.config import Settings
from app.llm.anthropic import AnthropicBackend
from app.llm.base import ProviderError, ProviderErrorCode
from app.llm.catalog import Availability, Cost, ModelRef
from app.llm.gemini import GeminiBackend
from app.llm.gemini import create_client as create_gemini_client
from app.llm.ollama import OllamaBackend
from app.llm.registry import LLMRegistry
from app.main import create_app
from tests.chat_fakes import FakeBackend, Gate, ScriptedProvider, cite_first_source, sources_in
from tests.chat_helpers import (
    answer_text,
    ask,
    ask_events,
    create_conversation,
    names,
    one,
    query_rows,
)
from tests.search_fixtures import SeedChunk, seed_document

pytestmark = pytest.mark.integration

REMOTE_EN = "Employees may work remotely up to three days per week with manager approval."
BACKUP_EN = "Database backups run nightly and are kept for thirty days."
SECRETS = ("sk-ant-secret-0001", "AIza-secret-0002")


@pytest.fixture
def docs(db_settings: Settings) -> dict[str, uuid.UUID]:
    doc = seed_document(
        db_settings,
        "policy.md",
        [SeedChunk(REMOTE_EN, page=3, section="Remote work"), SeedChunk(BACKUP_EN, page=7)],
    )
    return {"doc": doc.id, "remote": doc.chunk_ids[0], "backup": doc.chunk_ids[1]}


def fake_backends() -> dict[str, FakeBackend]:
    return {
        "anthropic": FakeBackend(
            "anthropic",
            [ScriptedProvider(cite_first_source, model="claude-opus-5-5")],
            label="Anthropic Claude",
        ),
        "gemini": FakeBackend(
            "gemini",
            [
                ScriptedProvider(cite_first_source, model="gemini-3.8-flash"),
                ScriptedProvider(cite_first_source, model="gemini-3.5-flash-lite"),
            ],
            cost=Cost.FREE_TIER,
            label="Google Gemini",
        ),
        "ollama": FakeBackend(
            "ollama",
            [
                ScriptedProvider(cite_first_source, model="qwen3:8b"),
                ScriptedProvider(cite_first_source, model="missing:1b"),
            ],
            cost=Cost.LOCAL,
            label="Ollama (local)",
            availability={"missing:1b": Availability.unavailable("Not installed in Ollama.")},
        ),
    }


@pytest.fixture
def make_client(db_settings: Settings) -> Callable[..., TestClient]:
    clients: list[TestClient] = []

    def _make(registry: LLMRegistry, **overrides: object) -> TestClient:
        settings = db_settings.model_copy(update=overrides) if overrides else db_settings
        client = TestClient(create_app(settings, llm_registry=registry))
        client.__enter__()
        clients.append(client)
        return client

    yield _make
    for client in clients:
        client.__exit__(None, None, None)


def _fake_client(
    make_client: Callable[..., TestClient], **changes: object
) -> tuple[TestClient, dict[str, FakeBackend]]:
    backends = fake_backends()
    for name, configured in changes.items():
        backends[name]._configured = configured  # type: ignore[attr-defined]
    registry = LLMRegistry(list(backends.values()), ModelRef("anthropic", "claude-opus-5-5"))
    return make_client(registry), backends


def _scripted(backends: dict[str, FakeBackend], provider: str, model: str) -> ScriptedProvider:
    return backends[provider]._providers[model]  # type: ignore[return-value]


def _choice(provider: str, model: str) -> dict:
    return {"model": {"provider": provider, "model": model}}


# --- the catalog -----------------------------------------------------------------------------


def test_models_endpoint(make_client: Callable[..., TestClient]) -> None:
    client, _ = _fake_client(make_client, ollama=False)
    catalog = client.get("/api/v1/chat/models").json()
    assert catalog["default"] == {"provider": "anthropic", "model": "claude-opus-5-5"}
    assert [(p["name"], p["cost"], p["configured"]) for p in catalog["providers"]] == [
        ("anthropic", "paid", True),
        ("gemini", "free_tier", True),
        ("ollama", "local", False),
    ]
    assert catalog["providers"][2]["reason"] == "OLLAMA_API_KEY is not set"
    models = {(m["provider"], m["model"]): m for m in catalog["models"]}
    assert models[("anthropic", "claude-opus-5-5")]["default"] is True
    assert models[("gemini", "gemini-3.8-flash")]["availability"]["state"] == "available"
    assert models[("gemini", "gemini-3.8-flash")]["selectable"] is True
    assert models[("ollama", "qwen3:8b")]["selectable"] is False
    assert models[("ollama", "qwen3:8b")]["availability"]["state"] == "unavailable"


def test_models_endpoint_with_real_backends_never_leaks_secrets(
    make_client: Callable[..., TestClient], db_settings: Settings
) -> None:
    settings = db_settings.model_copy(
        update={
            "anthropic_api_key": SecretStr(SECRETS[0]),
            "gemini_api_key": SecretStr(SECRETS[1]),
            "ollama_base_url": "http://ollama-secret-host.internal:11434",
            "ollama_models": ["qwen3:8b"],
            "llm_catalog_check_timeout_seconds": 0.5,
        }
    )
    backends = [
        AnthropicBackend(settings, _anthropic_client(lambda r: httpx2.Response(401, json={}))),
        GeminiBackend(settings, _gemini_client(settings, lambda r: httpx.Response(404, json={}))),
        OllamaBackend(settings, _ollama_client(lambda r: _raise(httpx2.ConnectError("no")))),
    ]
    client = make_client(LLMRegistry(backends, ModelRef("anthropic", "claude-opus-5-5")))
    response = client.get("/api/v1/chat/models")
    assert response.status_code == 200
    for secret in (*SECRETS, "ollama-secret-host"):
        assert secret not in response.text
    models = {(m["provider"], m["model"]): m for m in response.json()["models"]}
    assert models[("anthropic", "claude-opus-5-5")]["availability"]["state"] == "unavailable"
    assert models[("gemini", "gemini-3.8-flash")]["availability"]["state"] == "unavailable"
    ollama = models[("ollama", "qwen3:8b")]["availability"]
    assert ollama["state"] == "unavailable" and "not running" in ollama["reason"]


def test_options_report_the_default(make_client: Callable[..., TestClient]) -> None:
    client, _ = _fake_client(make_client)
    options = client.get("/api/v1/chat/options").json()
    assert (options["configured"], options["provider"], options["model"]) == (
        True,
        "anthropic",
        "claude-opus-5-5",
    )


# --- choosing a model ------------------------------------------------------------------------


def test_chosen_model_answers_and_both_choices_are_stored(
    make_client: Callable[..., TestClient], docs: dict, db_settings: Settings
) -> None:
    client, backends = _fake_client(make_client)
    conversation = create_conversation(client)
    events = ask_events(
        client, conversation, "remote work?", **_choice("gemini", "gemini-3.8-flash")
    )
    start = one(events, "start")
    assert (start["provider"], start["model"]) == ("gemini", "gemini-3.8-flash")
    assert one(events, "done")["status"] == "complete"
    assert len(_scripted(backends, "gemini", "gemini-3.8-flash").requests) == 1
    assert not _scripted(backends, "anthropic", "claude-opus-5-5").requests
    rows = query_rows(
        db_settings,
        "SELECT requested_provider, requested_model, provider, model, served_model "
        "FROM messages WHERE role = 'assistant'",
    )
    assert [tuple(r) for r in rows] == [
        ("gemini", "gemini-3.8-flash", "gemini", "gemini-3.8-flash", "gemini-3.8-flash")
    ]
    answer = client.get(f"/api/v1/conversations/{conversation}").json()["messages"][1]
    assert (answer["requested_provider"], answer["requested_model"]) == (
        "gemini",
        "gemini-3.8-flash",
    )


def test_no_model_uses_the_default(make_client: Callable[..., TestClient], docs: dict) -> None:
    client, backends = _fake_client(make_client)
    events = ask_events(client, create_conversation(client), "remote work?")
    assert one(events, "start")["provider"] == "anthropic"
    assert len(_scripted(backends, "anthropic", "claude-opus-5-5").requests) == 1


def test_switching_models_within_a_conversation(
    make_client: Callable[..., TestClient], docs: dict
) -> None:
    client, backends = _fake_client(make_client)
    conversation = create_conversation(client)
    turns = [
        ("gemini", "gemini-3.8-flash", "How many days can I work remotely?"),
        ("ollama", "qwen3:8b", "How long are database backups kept?"),
        ("anthropic", "claude-opus-5-5", "And who approves remote work?"),
        ("gemini", "gemini-3.5-flash-lite", "How many days remotely again?"),
    ]
    for provider, model, question in turns:
        events = ask_events(client, conversation, question, **_choice(provider, model))
        assert one(events, "done")["status"] == "complete"
        assert one(events, "start")["model"] == model

    # Each provider saw the earlier turns, whichever model answered them.
    claude = _scripted(backends, "anthropic", "claude-opus-5-5").requests[0]
    history = [(m.role, m.content) for m in claude.messages[:-1]]
    assert [role for role, _ in history] == ["user", "assistant", "user", "assistant"]
    assert history[0][1] == turns[0][2] and history[2][1] == turns[1][2]

    detail = client.get(f"/api/v1/conversations/{conversation}").json()
    answers = [m for m in detail["messages"] if m["role"] == "assistant"]
    assert [(a["provider"], a["model"]) for a in answers] == [(p, m) for p, m, _ in turns]
    # Citations of every answer still point at the passages that answer was given.
    for answer in answers:
        supporting = [c for c in answer["citations"] if c["supporting"]]
        assert supporting and all(c["source_status"] == "available" for c in supporting)
        assert answer["content"].endswith("[S1].")
        cited = supporting[0]
        assert cited["excerpt"] in answer["content"]


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        (_choice("openai", "gpt-5"), 422, "invalid_model"),
        (_choice("gemini", "gemini-1.0-pro"), 422, "invalid_model"),
        (_choice("anthropic", "gemini-3.8-flash"), 422, "invalid_model"),
        (_choice("ollama", "missing:1b"), 503, "model_unavailable"),
        ({"model": {"provider": "gemini"}}, 422, "validation_error"),
        (
            {"model": {"provider": "gemini", "model": "x", "base_url": "http://evil"}},
            422,
            "validation_error",
        ),
        ({"model": {"provider": "Gemini!", "model": "x"}}, 422, "validation_error"),
        ({"model": {"provider": "ollama", "model": "http://evil.test/m"}}, 422, "invalid_model"),
        ({"model": "gemini-3.8-flash"}, 422, "validation_error"),
    ],
)
def test_invalid_or_unavailable_choices_are_rejected_before_storing(
    make_client: Callable[..., TestClient],
    docs: dict,
    db_settings: Settings,
    body: dict,
    status: int,
    code: str,
) -> None:
    client, backends = _fake_client(make_client)
    conversation = create_conversation(client)
    response = ask(client, conversation, "remote work?", **body)
    assert response.status_code == status, response.text
    assert response.json()["error"]["code"] == code
    assert query_rows(db_settings, "SELECT count(*) FROM messages")[0][0] == 0
    assert all(not p.requests for b in backends.values() for p in b._providers.values())


def test_unconfigured_provider_is_503(
    make_client: Callable[..., TestClient], docs: dict, db_settings: Settings
) -> None:
    client, _ = _fake_client(make_client, gemini=False)
    response = ask(
        client, create_conversation(client), "q?", **_choice("gemini", "gemini-3.8-flash")
    )
    assert response.status_code == 503
    assert response.json()["error"]["code"] == "provider_not_configured"
    assert "GEMINI_API_KEY is not set" in response.json()["error"]["message"]
    assert query_rows(db_settings, "SELECT count(*) FROM messages")[0][0] == 0


def test_a_failing_free_model_never_falls_back_to_a_paid_one(
    make_client: Callable[..., TestClient], docs: dict, db_settings: Settings
) -> None:
    client, backends = _fake_client(make_client)
    gemini = _scripted(backends, "gemini", "gemini-3.8-flash")
    gemini.script = [
        ProviderError(
            ProviderErrorCode.RATE_LIMITED, "Gemini's rate limit was reached.", True, 37.0
        )
    ]
    events = ask_events(
        client, create_conversation(client), "remote work?", **_choice("gemini", "gemini-3.8-flash")
    )
    assert names(events) == ["start", "sources", "error"]
    error = one(events, "error")
    assert (error["code"], error["retry_after_seconds"]) == ("provider_rate_limited", 37.0)
    assert all(
        not p.requests
        for name, b in backends.items()
        for p in b._providers.values()
        if name != "gemini"
    )
    rows = query_rows(
        db_settings,
        "SELECT status, requested_provider, provider, error_code FROM messages "
        "WHERE role = 'assistant'",
    )
    assert [tuple(r) for r in rows] == [("failed", "gemini", "gemini", "provider_rate_limited")]


def test_retry_with_another_model_replaces_the_failed_answer(
    make_client: Callable[..., TestClient], docs: dict, db_settings: Settings
) -> None:
    client, backends = _fake_client(make_client)
    _scripted(backends, "gemini", "gemini-3.8-flash").script = [
        ProviderError(ProviderErrorCode.OVERLOADED, "Gemini is overloaded.", True)
    ]
    conversation = create_conversation(client)
    key = str(uuid.uuid4())
    first = ask_events(
        client,
        conversation,
        "remote work?",
        client_message_id=key,
        **_choice("gemini", "gemini-3.8-flash"),
    )
    assert names(first)[-1] == "error"
    second = ask_events(
        client,
        conversation,
        "remote work?",
        client_message_id=key,
        **_choice("ollama", "qwen3:8b"),
    )
    assert one(second, "done")["status"] == "complete"
    rows = query_rows(
        db_settings,
        "SELECT sequence, role, requested_provider, requested_model, status FROM messages "
        "ORDER BY sequence",
    )
    assert [tuple(r) for r in rows] == [
        (1, "user", None, None, "complete"),
        (2, "assistant", "ollama", "qwen3:8b", "complete"),
    ]
    # A completed answer is replayed as stored, whatever model the retry names.
    replay = ask_events(
        client,
        conversation,
        "remote work?",
        client_message_id=key,
        **_choice("anthropic", "claude-opus-5-5"),
    )
    assert one(replay, "start")["replayed"] is True
    assert one(replay, "start")["model"] == "qwen3:8b"
    assert not _scripted(backends, "anthropic", "claude-opus-5-5").requests


def test_no_sources_keeps_the_requested_model(
    make_client: Callable[..., TestClient], db_settings: Settings
) -> None:
    client, backends = _fake_client(make_client)
    events = ask_events(
        client, create_conversation(client), "anything?", **_choice("ollama", "qwen3:8b")
    )
    assert one(events, "done")["grounding"] == "no_sources"
    assert not _scripted(backends, "ollama", "qwen3:8b").requests
    rows = query_rows(
        db_settings,
        "SELECT requested_provider, requested_model, provider, model FROM messages "
        "WHERE role = 'assistant'",
    )
    assert [tuple(r) for r in rows] == [("ollama", "qwen3:8b", None, None)]


def test_cancelling_an_answer_from_a_chosen_model(
    make_client: Callable[..., TestClient], docs: dict, db_settings: Settings
) -> None:
    from app.api.v1.conversations import _sse_stream
    from app.chat.service import AskRequest, RetrievalOptions

    client, backends = _fake_client(make_client)
    gate = Gate.closed()
    ollama = _scripted(backends, "ollama", "qwen3:8b")
    ollama.script = ["Partial [S1] ", gate, "never sent"]
    conversation = create_conversation(client)
    app = client.app  # type: ignore[attr-defined]

    async def disconnect() -> None:
        stream = _sse_stream(
            app.state.chat.answer(
                AskRequest(
                    uuid.UUID(conversation),
                    "remote work?",
                    None,
                    RetrievalOptions(),
                    model=ModelRef("ollama", "qwen3:8b"),
                )
            ),
            "req-1",
            15.0,
            app.state.chat_tasks,
        )
        async for chunk in stream:
            if b"event: delta" in chunk:
                break
        await stream.aclose()
        await asyncio.wait(set(app.state.chat_tasks), timeout=5)

    client.portal.call(disconnect)  # type: ignore[attr-defined]
    gate.event.set()
    rows = query_rows(
        db_settings,
        "SELECT status, content, requested_model, model FROM messages WHERE role = 'assistant'",
    )
    assert [tuple(r) for r in rows] == [("cancelled", "Partial [S1] ", "qwen3:8b", "qwen3:8b")]
    assert ollama.closed_early == 1


# --- the real provider implementations over mock transports ---------------------------------


def _raise(error: Exception) -> httpx2.Response:
    raise error


def _anthropic_client(
    handler: Callable[[httpx2.Request], httpx2.Response],
) -> anthropic.AsyncAnthropic:
    return anthropic.AsyncAnthropic(
        api_key=SECRETS[0],
        base_url="http://anthropic.test",
        max_retries=0,
        http_client=anthropic.DefaultAsyncHttpxClient(transport=httpx2.MockTransport(handler)),
    )


def _gemini_client(settings: Settings, handler: Callable[[httpx.Request], httpx.Response]):  # type: ignore[no-untyped-def]
    return create_gemini_client(
        settings,
        retry_initial_delay=0.01,
        base_url="http://gemini.test",
        httpx_async_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


def _ollama_client(handler: Callable[[httpx2.Request], httpx2.Response]) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        base_url="http://ollama.test:11434", transport=httpx2.MockTransport(handler)
    )


def _cited(prompt: str) -> str:
    """Answer like a model would: quote the first numbered source and cite it."""
    first = sources_in_text(prompt)[1].split("\n")[0][:200]
    return f"Per the policy: {first} [S1]."


def sources_in_text(prompt: str) -> dict[int, str]:
    from app.llm.base import GenerationRequest, PromptMessage

    return sources_in(GenerationRequest("", (PromptMessage("user", prompt),), 1))


def _anthropic_handler(seen: list[dict]) -> Callable[[httpx2.Request], httpx2.Response]:
    from tests.test_llm_anthropic import _stream_body

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.startswith("/v1/models/"):
            model = request.url.path.rsplit("/", 1)[-1]
            return httpx2.Response(
                200,
                json={
                    "type": "model",
                    "id": model,
                    "display_name": model,
                    "created_at": "2026-01-01T00:00:00Z",
                },
            )
        body = json.loads(request.content)
        seen.append(body)
        text = _cited(body["messages"][-1]["content"])
        return httpx2.Response(
            200,
            content=_stream_body([text[:20], text[20:]], model=body["model"]),
            headers={"content-type": "text/event-stream"},
        )

    return handler


def _gemini_handler(seen: list[dict]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET":
            return httpx.Response(200, json={"name": request.url.path.split("/", 2)[-1]})
        body = json.loads(request.content)
        seen.append(body)
        text = _cited(body["contents"][-1]["parts"][0]["text"])
        chunks = [
            {
                "candidates": [
                    {
                        "content": {
                            "role": "model",
                            "parts": [{"text": "thoughts", "thought": True}, {"text": text[:15]}],
                        }
                    }
                ],
                "modelVersion": "gemini-3.8-flash",
                "responseId": "g-1",
            },
            {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": text[15:]}]},
                        "finishReason": "STOP",
                    }
                ],
                "usageMetadata": {
                    "promptTokenCount": 900,
                    "candidatesTokenCount": 40,
                    "thoughtsTokenCount": 10,
                },
                "modelVersion": "gemini-3.8-flash",
                "responseId": "g-1",
            },
        ]
        content = "".join(f"data: {json.dumps(c)}\r\n\r\n" for c in chunks).encode()
        return httpx.Response(200, content=content, headers={"content-type": "text/event-stream"})

    return handler


def _ollama_handler(seen: list[dict]) -> Callable[[httpx2.Request], httpx2.Response]:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/tags":
            return httpx2.Response(200, json={"models": [{"name": "qwen3:8b"}]})
        body = json.loads(request.content)
        seen.append(body)
        text = _cited(body["messages"][-1]["content"])
        lines = [
            {
                "model": "qwen3:8b",
                "message": {"role": "assistant", "content": text[:10]},
                "done": False,
            },
            {
                "model": "qwen3:8b",
                "message": {"role": "assistant", "content": text[10:]},
                "done": False,
            },
            {
                "model": "qwen3:8b",
                "message": {"role": "assistant", "content": ""},
                "done": True,
                "done_reason": "stop",
                "prompt_eval_count": 950,
                "eval_count": 45,
            },
        ]
        return httpx2.Response(200, content="".join(json.dumps(x) + "\n" for x in lines).encode())

    return handler


def test_each_real_provider_streams_cited_answers_in_one_conversation(
    make_client: Callable[..., TestClient], docs: dict, db_settings: Settings
) -> None:
    settings = db_settings.model_copy(
        update={
            "anthropic_api_key": SecretStr(SECRETS[0]),
            "gemini_api_key": SecretStr(SECRETS[1]),
            "ollama_base_url": "http://ollama.test:11434",
            "ollama_models": ["qwen3:8b"],
        }
    )
    seen: dict[str, list[dict]] = {"anthropic": [], "gemini": [], "ollama": []}
    registry = LLMRegistry(
        [
            AnthropicBackend(settings, _anthropic_client(_anthropic_handler(seen["anthropic"]))),
            GeminiBackend(settings, _gemini_client(settings, _gemini_handler(seen["gemini"]))),
            OllamaBackend(settings, _ollama_client(_ollama_handler(seen["ollama"]))),
        ],
        ModelRef("anthropic", "claude-opus-5-5"),
    )
    client = make_client(registry)
    conversation = create_conversation(client)
    expected = f"Per the policy: {REMOTE_EN} [S1]."
    for provider, model in [
        ("gemini", "gemini-3.8-flash"),
        ("ollama", "qwen3:8b"),
        ("anthropic", "claude-sonnet-5-5"),
    ]:
        events = ask_events(
            client,
            conversation,
            "How many days can I work remotely?",
            retrieval={"mode": "keyword"},
            **_choice(provider, model),
        )
        assert names(events) == ["start", "sources", "delta", "citations", "usage", "done"]
        assert answer_text(events) == expected
        citations = one(events, "citations")["citations"]
        assert citations[0]["supporting"] and citations[0]["chunk_id"] == str(docs["remote"])
        assert one(events, "done")["grounding"] == "grounded"
        assert len(seen[provider]) == 1

    # Each provider got the same grounding prompt in its own wire format.
    assert seen["anthropic"][0]["model"] == "claude-sonnet-5-5"
    assert "<question>" in seen["anthropic"][0]["messages"][-1]["content"]
    assert seen["gemini"][0]["systemInstruction"]["parts"][0]["text"] == SYSTEM_PROMPT
    assert seen["ollama"][0]["messages"][0]["role"] == "system"
    # History crosses providers: Claude saw the Gemini and Ollama answers.
    assert len(seen["anthropic"][0]["messages"]) == 5

    detail = client.get(f"/api/v1/conversations/{conversation}").json()
    answers = [m for m in detail["messages"] if m["role"] == "assistant"]
    assert [(a["provider"], a["model"], a["served_model"]) for a in answers] == [
        ("gemini", "gemini-3.8-flash", "gemini-3.8-flash"),
        ("ollama", "qwen3:8b", "qwen3:8b"),
        ("anthropic", "claude-sonnet-5-5", "claude-sonnet-5-5"),
    ]
    assert [a["usage"]["output_tokens"] for a in answers] == [50, 45, 42]
    for secret in SECRETS:
        assert secret not in json.dumps(detail)


def test_real_ollama_provider_cancellation_closes_the_request(
    make_client: Callable[..., TestClient], docs: dict, db_settings: Settings
) -> None:
    from app.api.v1.conversations import _sse_stream
    from app.chat.service import AskRequest, RetrievalOptions

    settings = db_settings.model_copy(
        update={"ollama_base_url": "http://ollama.test:11434", "ollama_models": ["qwen3:8b"]}
    )
    release = threading.Event()
    state = {"closed": False}

    class Body(httpx2.AsyncByteStream):
        async def __aiter__(self):  # type: ignore[no-untyped-def]
            yield (
                json.dumps(
                    {"model": "qwen3:8b", "message": {"content": "Partial [S1] "}, "done": False}
                )
                + "\n"
            ).encode()
            while not release.is_set():
                await asyncio.sleep(0.01)
            yield b'{"model":"qwen3:8b","message":{"content":"never"},"done":false}\n'

        async def aclose(self) -> None:
            state["closed"] = True

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == "/api/tags":
            return httpx2.Response(200, json={"models": [{"name": "qwen3:8b"}]})
        return httpx2.Response(200, stream=Body())

    registry = LLMRegistry(
        [OllamaBackend(settings, _ollama_client(handler))], ModelRef("ollama", "qwen3:8b")
    )
    client = make_client(registry)
    conversation = create_conversation(client)
    app = client.app  # type: ignore[attr-defined]

    async def disconnect() -> None:
        stream = _sse_stream(
            app.state.chat.answer(
                AskRequest(uuid.UUID(conversation), "remote work?", None, RetrievalOptions())
            ),
            "req-1",
            15.0,
            app.state.chat_tasks,
        )
        async for chunk in stream:
            if b"event: delta" in chunk:
                break
        await stream.aclose()
        await asyncio.wait(set(app.state.chat_tasks), timeout=5)

    try:
        client.portal.call(disconnect)  # type: ignore[attr-defined]
    finally:
        release.set()
    assert state["closed"] is True
    rows = query_rows(
        db_settings, "SELECT status, content, model FROM messages WHERE role = 'assistant'"
    )
    assert [tuple(r) for r in rows] == [("cancelled", "Partial [S1] ", "qwen3:8b")]
