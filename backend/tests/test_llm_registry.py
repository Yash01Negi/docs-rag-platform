"""The provider registry and model catalog: selection rules, caching, configuration."""

import asyncio
from collections.abc import Callable

import pytest

from app.core.config import Settings
from app.llm import create_llm_registry
from app.llm.catalog import Availability, AvailabilityState, Cost, ModelRef
from app.llm.registry import LLMRegistry, ProviderBackend, SelectionError
from tests.chat_fakes import FakeBackend, ScriptedProvider


def _registry(**kwargs: object) -> tuple[LLMRegistry, dict[str, FakeBackend]]:
    backends = {
        "anthropic": FakeBackend(
            "anthropic", [ScriptedProvider(model="claude-a"), ScriptedProvider(model="claude-b")]
        ),
        "gemini": FakeBackend(
            "gemini",
            [ScriptedProvider(model="gemini-flash"), ScriptedProvider(model="gemini-lite")],
            cost=Cost.FREE_TIER,
            availability={"gemini-lite": Availability.unavailable("Google does not list it.")},
        ),
        "ollama": FakeBackend(
            "ollama", [ScriptedProvider(model="qwen3:8b")], cost=Cost.LOCAL, configured=False
        ),
    }
    registry = LLMRegistry(
        list(backends.values()),
        ModelRef("anthropic", "claude-a"),
        **kwargs,  # type: ignore[arg-type]
    )
    return registry, backends


def _error(call: Callable[[], object]) -> SelectionError:
    with pytest.raises(SelectionError) as info:
        call()
    return info.value


def test_resolve_default_and_explicit_choices() -> None:
    registry, _ = _registry()
    assert registry.resolve(None).model == "claude-a"
    provider = registry.resolve(ModelRef("gemini", "gemini-flash"))
    assert (provider.name, provider.model) == ("gemini", "gemini-flash")
    assert registry.resolve(ModelRef("anthropic", "claude-b")).model == "claude-b"


@pytest.mark.parametrize(
    "ref",
    [
        ModelRef("openai", "gpt-x"),  # unknown provider
        ModelRef("gemini", "gemini-9-ultra"),  # not in the allowlist
        ModelRef("anthropic", "gemini-flash"),  # a model of another provider
        ModelRef("ollama", "http://evil.test/model"),  # never treated as a URL
    ],
)
def test_choices_outside_the_catalog_are_rejected(ref: ModelRef) -> None:
    registry, backends = _registry()
    error = _error(lambda: registry.resolve(ref))
    assert (error.status_code, error.code) == (422, "invalid_model")
    assert ref.model not in error.message  # the client's text is not echoed back
    assert all(not p.requests for b in backends.values() for p in b._providers.values())


def test_unconfigured_provider() -> None:
    registry, _ = _registry()
    error = _error(lambda: registry.resolve(ModelRef("ollama", "qwen3:8b")))
    assert (error.status_code, error.code) == (503, "provider_not_configured")
    assert "OLLAMA_API_KEY is not set" in error.message


def test_unavailable_model_is_refused_once_checked() -> None:
    registry, backends = _registry()
    ref = ModelRef("gemini", "gemini-lite")
    assert registry.resolve(ref).model == "gemini-lite"  # not checked yet: allowed
    error = _error(lambda: asyncio.run(registry.select(ref)))
    assert (error.status_code, error.code) == (503, "model_unavailable")
    assert "Google does not list it." in error.message
    assert backends["gemini"].checks == 1
    # Other models of the same provider are unaffected; nothing is substituted.
    assert asyncio.run(registry.select(ModelRef("gemini", "gemini-flash"))).model == "gemini-flash"
    assert backends["gemini"].checks == 1  # the cached check is reused


def test_default_not_configured_keeps_phase_4_error() -> None:
    backend = FakeBackend("anthropic", [ScriptedProvider(model="claude-a")], configured=False)
    registry = LLMRegistry([backend], ModelRef("anthropic", "claude-a"))
    error = _error(lambda: registry.resolve(None))
    assert (error.status_code, error.code) == (503, "llm_not_configured")
    assert "ANTHROPIC_API_KEY is not set" in error.message
    assert registry.default_provider() is None and not registry.any_configured


def test_nothing_configured() -> None:
    registry = LLMRegistry.single(None)
    assert _error(lambda: registry.resolve(None)).code == "llm_not_configured"
    assert asyncio.run(registry.catalog()) == ([], [])


def test_catalog_reports_every_model() -> None:
    registry, _ = _registry()
    providers, models = asyncio.run(registry.catalog())
    assert [(p.name, p.cost, p.configured) for p in providers] == [
        ("anthropic", Cost.PAID, True),
        ("gemini", Cost.FREE_TIER, True),
        ("ollama", Cost.LOCAL, False),
    ]
    by_id = {(m.provider, m.model): m for m in models}
    assert by_id[("anthropic", "claude-a")].default
    assert by_id[("anthropic", "claude-a")].availability.state is AvailabilityState.AVAILABLE
    lite = by_id[("gemini", "gemini-lite")]
    assert lite.availability.state is AvailabilityState.UNAVAILABLE and not lite.selectable
    ollama = by_id[("ollama", "qwen3:8b")]
    assert not ollama.configured and not ollama.selectable
    assert ollama.availability.reason == "OLLAMA_API_KEY is not set"


def test_checks_are_cached_and_refresh_is_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr("app.llm.registry.time.monotonic", lambda: now[0])
    registry, backends = _registry(ttl_seconds=300)
    asyncio.run(registry.catalog())
    asyncio.run(registry.catalog(refresh=True))  # too soon after the last check
    assert backends["anthropic"].checks == 1
    now[0] += 11
    asyncio.run(registry.catalog(refresh=True))
    assert backends["anthropic"].checks == 2
    now[0] += 299
    asyncio.run(registry.catalog())
    assert backends["anthropic"].checks == 2
    now[0] += 2
    asyncio.run(registry.catalog())
    assert backends["anthropic"].checks == 3
    assert backends["ollama"].checks == 0  # never checked while not configured


def test_transient_results_are_rechecked_sooner(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]
    monkeypatch.setattr("app.llm.registry.time.monotonic", lambda: now[0])
    registry, backends = _registry(ttl_seconds=300)
    backends["anthropic"].availability = {
        "claude-a": Availability.unavailable("not reachable", transient=True)
    }
    asyncio.run(registry.catalog())
    now[0] += 29
    asyncio.run(registry.catalog())
    assert backends["anthropic"].checks == 1
    now[0] += 2
    asyncio.run(registry.catalog())
    assert backends["anthropic"].checks == 2


class _Broken(ProviderBackend):
    name = "broken"
    label = "Broken"

    def __init__(self, slow: bool) -> None:
        super().__init__(["m"])
        self.slow = slow

    @property
    def configured(self) -> bool:
        return True

    @property
    def cost(self) -> Cost:
        return Cost.PAID

    def _create(self, model: str):  # type: ignore[no-untyped-def]
        return ScriptedProvider(name="broken", model=model)

    async def check(self) -> dict[str, Availability]:
        if self.slow:
            await asyncio.sleep(10)
        raise RuntimeError("secret detail")


@pytest.mark.parametrize("slow", [True, False])
def test_failing_checks_are_unknown(slow: bool) -> None:
    registry = LLMRegistry([_Broken(slow)], None, check_timeout_seconds=0.05)
    _, (model,) = asyncio.run(registry.catalog())
    assert model.availability.state is AvailabilityState.UNKNOWN and model.selectable
    assert "secret" not in (model.availability.reason or "")


def test_registry_from_settings(make_settings: Callable[..., Settings]) -> None:
    registry = create_llm_registry(
        make_settings(
            gemini_api_key="AIza-x",
            ollama_base_url="http://host.docker.internal:11434",
            ollama_models="qwen3:8b",
        )
    )
    assert registry.default == ModelRef("anthropic", "claude-opus-5-5")
    assert {n: b.configured for n, b in registry.backends.items()} == {
        "anthropic": False,
        "gemini": True,
        "ollama": True,
    }
    # The default (Claude) has no key: a request without a model is not silently answered
    # by another provider.
    assert _error(lambda: registry.resolve(None)).code == "llm_not_configured"
    assert registry.resolve(ModelRef("gemini", "gemini-3.8-flash")).name == "gemini"
    assert registry.resolve(ModelRef("ollama", "qwen3:8b")).name == "ollama"


def test_default_model_settings(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings(llm_provider="gemini", gemini_api_key="AIza-x")
    assert settings.default_model == "gemini-3.8-flash"
    custom = make_settings(llm_model="claude-sonnet-4-6")
    # A model named only in LLM_MODEL stays selectable and is the default, as in Phase 4.
    assert custom.provider_models("anthropic")[0] == "claude-sonnet-4-6"
    assert custom.default_model == "claude-sonnet-4-6"
    listed = make_settings(llm_model="claude-haiku-5-5")
    assert listed.provider_models("anthropic") == [
        "claude-opus-5-5",
        "claude-sonnet-5-5",
        "claude-haiku-5-5",
    ]
    assert listed.default_model == "claude-haiku-5-5"
    assert make_settings(llm_provider="ollama").default_model is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"gemini_models": "gemini 3 flash"},
        {"anthropic_models": "claude;rm -rf"},
        {"ollama_models": "../etc/passwd"},
        {"ollama_models": ",".join(f"m{i}" for i in range(21))},
        {"llm_model": "bad model"},
        {"llm_provider": "openai"},
        {"gemini_tier": "enterprise"},
        {"gemini_thinking_level": "max"},
        {"ollama_keep_alive": "forever"},
        {"ollama_num_ctx": 100},
    ],
)
def test_invalid_model_settings(make_settings: Callable[..., Settings], overrides: dict) -> None:
    with pytest.raises(ValueError):
        make_settings(**overrides)


def test_model_lists_are_parsed_and_deduplicated(make_settings: Callable[..., Settings]) -> None:
    settings = make_settings(gemini_models=" gemini-a , gemini-b,gemini-a,, ")
    assert settings.gemini_models == ["gemini-a", "gemini-b"]
    assert make_settings(ollama_models="library/qwen3:8b").ollama_models == ["library/qwen3:8b"]


def test_empty_values_mean_unset(make_settings: Callable[..., Settings]) -> None:
    # Compose passes `${LLM_MODEL:-}` and .env.example has `OLLAMA_BASE_URL=`.
    settings = make_settings(
        llm_model="",
        ollama_base_url="",
        ollama_models="",
        gemini_thinking_level="",
        ollama_think="",
        ollama_keep_alive=" ",
    )
    assert settings.llm_model is None and settings.default_model == "claude-opus-5-5"
    assert settings.ollama_base_url is None and not settings.ollama_configured
    assert settings.gemini_thinking_level is None and settings.ollama_think is None
    assert settings.ollama_keep_alive is None
