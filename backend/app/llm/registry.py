"""The provider registry: one backend per provider, one :class:`LLMProvider` per model.

A :class:`ProviderBackend` owns a provider's network client (created once at startup and
shared by all of its models), knows which models are configured for it, and can ask the
provider which of them are actually available. :class:`LLMRegistry` puts the backends
together into the model catalog and resolves a chat request's model choice:

* a provider or model that is not in the catalog is rejected (422 ``invalid_model``);
* a provider without credentials answers 503 ``provider_not_configured``;
* a model the provider said is unavailable answers 503 ``model_unavailable``.

Nothing here ever substitutes another model or provider for the one that was asked for.
"""

from __future__ import annotations

import asyncio
import logging
import time
from abc import ABC, abstractmethod
from collections.abc import Sequence

from app.core.config import Settings
from app.llm.base import LLMProvider
from app.llm.catalog import (
    Availability,
    AvailabilityState,
    Cost,
    ModelInfo,
    ModelRef,
    ProviderInfo,
)

logger = logging.getLogger(__name__)

# Fewest seconds between two forced re-checks of the same provider (?refresh=true).
MIN_REFRESH_SECONDS = 10.0
# How long a check that could not reach the provider is reused.
TRANSIENT_TTL_SECONDS = 30.0


class SelectionError(Exception):
    """A model choice that cannot be served, with an HTTP status and a user-safe message."""

    def __init__(self, status_code: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


class ProviderBackend(ABC):
    """A provider's shared client, configured models and availability check."""

    name: str
    label: str

    def __init__(self, models: Sequence[str]) -> None:
        self.models: tuple[str, ...] = tuple(models)
        self._providers: dict[str, LLMProvider] = {}

    @property
    @abstractmethod
    def configured(self) -> bool:
        """True when the provider has what it needs to be called (a key, a base URL)."""

    @property
    @abstractmethod
    def cost(self) -> Cost: ...

    @property
    def not_configured_reason(self) -> str | None:
        """Why the provider cannot be used, for the catalog and for 503 responses."""
        return None

    @property
    def notice(self) -> str | None:
        """What users should know before choosing one of the provider's models."""
        return None

    def provider(self, model: str) -> LLMProvider:
        """The (cached) provider for one of this backend's models."""
        if model not in self.models:
            raise KeyError(model)
        if model not in self._providers:
            self._providers[model] = self._create(model)
        return self._providers[model]

    @abstractmethod
    def _create(self, model: str) -> LLMProvider: ...

    @abstractmethod
    async def check(self) -> dict[str, Availability]:
        """Ask the provider which configured models are usable. Never raises."""

    async def aclose(self) -> None:  # noqa: B027  # optional hook
        """Release the shared client at shutdown."""


def _because(backend: ProviderBackend) -> str:
    reason = backend.not_configured_reason
    return f" ({reason})." if reason else "."


class StaticBackend(ProviderBackend):
    """A backend around one ready-made provider (tests, the evaluation harness)."""

    def __init__(self, provider: LLMProvider, cost: Cost = Cost.PAID) -> None:
        super().__init__([provider.model])
        self.name = provider.name
        self.label = provider.name
        self._cost = cost
        self._providers[provider.model] = provider

    @property
    def configured(self) -> bool:
        return True

    @property
    def cost(self) -> Cost:
        return self._cost

    def _create(self, model: str) -> LLMProvider:  # pragma: no cover - always cached
        raise KeyError(model)

    async def check(self) -> dict[str, Availability]:
        return {model: Availability.unknown() for model in self.models}

    async def aclose(self) -> None:
        for provider in self._providers.values():
            await provider.aclose()


class LLMRegistry:
    def __init__(
        self,
        backends: Sequence[ProviderBackend],
        default: ModelRef | None,
        *,
        ttl_seconds: float = 300.0,
        check_timeout_seconds: float = 5.0,
    ) -> None:
        self.backends: dict[str, ProviderBackend] = {b.name: b for b in backends}
        self.default = default
        self.ttl_seconds = ttl_seconds
        self.check_timeout_seconds = check_timeout_seconds
        self._checks: dict[str, tuple[float, dict[str, Availability]]] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @classmethod
    def from_settings(cls, settings: Settings) -> LLMRegistry:
        # Imported here so each SDK is only loaded by the application that uses it.
        from app.llm.anthropic import AnthropicBackend
        from app.llm.gemini import GeminiBackend
        from app.llm.ollama import OllamaBackend

        backends: list[ProviderBackend] = [
            AnthropicBackend(settings),
            GeminiBackend(settings),
            OllamaBackend(settings),
        ]
        model = settings.default_model
        return cls(
            backends,
            ModelRef(settings.llm_provider, model) if model else None,
            ttl_seconds=settings.llm_catalog_ttl_seconds,
            check_timeout_seconds=settings.llm_catalog_check_timeout_seconds,
        )

    @classmethod
    def single(cls, provider: LLMProvider | None, cost: Cost = Cost.PAID) -> LLMRegistry:
        """A registry offering exactly one provider (or nothing)."""
        if provider is None:
            return cls([], None)
        return cls([StaticBackend(provider, cost)], ModelRef(provider.name, provider.model))

    # --- resolving a request's model choice ----------------------------------------------

    @property
    def any_configured(self) -> bool:
        return any(b.configured and b.models for b in self.backends.values())

    def default_provider(self) -> LLMProvider | None:
        """The default model's provider, or None when it is not configured."""
        try:
            return self.resolve(None)
        except SelectionError:
            return None

    def resolve(self, ref: ModelRef | None) -> LLMProvider:
        """The provider for a request's choice (or the default). Raises SelectionError."""
        if ref is None:
            ref = self.default
            if ref is None or ref.provider not in self.backends:
                raise SelectionError(
                    503, "llm_not_configured", "Answer generation is not configured on the server."
                )
            backend = self.backends[ref.provider]
            if not backend.configured:
                # Phase 4 wording: the default provider has no credentials.
                raise SelectionError(
                    503,
                    "llm_not_configured",
                    "Answer generation is not configured on the server" + _because(backend),
                )
        backend = self.backends.get(ref.provider)
        if backend is None or ref.model not in backend.models:
            raise SelectionError(
                422,
                "invalid_model",
                "The selected model is not one of the models this server offers. Choose a model "
                "from GET /chat/models.",
            )
        if not backend.configured:
            raise SelectionError(
                503,
                "provider_not_configured",
                f"{backend.label} is not configured on the server" + _because(backend),
            )
        cached = self._checks.get(backend.name)
        if cached is not None and self._fresh(cached[0]):
            availability = cached[1].get(ref.model)
            if availability is not None and availability.state is AvailabilityState.UNAVAILABLE:
                raise SelectionError(
                    503,
                    "model_unavailable",
                    f"{ref.model} is not available: {availability.reason or 'unknown reason'}",
                )
        return backend.provider(ref.model)

    async def select(self, ref: ModelRef | None) -> LLMProvider:
        """Like :meth:`resolve`, after making sure the provider's availability is known.

        Uses the cached check when it is fresh, so asking costs at most one model-list
        request per provider and TTL.
        """
        provider = self.resolve(ref)
        await self._availability(self.backends[provider.name], refresh=False)
        return self.resolve(ref)

    # --- the catalog -------------------------------------------------------------------------

    def _fresh(self, checked_at: float) -> bool:
        return time.monotonic() - checked_at < self.ttl_seconds

    async def _availability(
        self, backend: ProviderBackend, refresh: bool
    ) -> dict[str, Availability]:
        if not backend.configured or not backend.models:
            return {}
        lock = self._locks.setdefault(backend.name, asyncio.Lock())
        async with lock:
            cached = self._checks.get(backend.name)
            if cached is not None:
                age = time.monotonic() - cached[0]
                if age < self.ttl_seconds and not (refresh and age >= MIN_REFRESH_SECONDS):
                    return cached[1]
            try:
                async with asyncio.timeout(self.check_timeout_seconds):
                    result = await backend.check()
            except TimeoutError:
                result = {
                    m: Availability.unknown("The provider did not answer the check in time.")
                    for m in backend.models
                }
            except Exception:
                logger.exception("llm_catalog_check_failed", extra={"provider": backend.name})
                result = {m: Availability.unknown("The check failed.") for m in backend.models}
            # Results that may change any moment are kept only briefly, so a provider that
            # comes up (Ollama started, network back) is noticed soon.
            transient = any(a.transient for a in result.values())
            checked_at = time.monotonic() - (
                max(0.0, self.ttl_seconds - TRANSIENT_TTL_SECONDS) if transient else 0.0
            )
            self._checks[backend.name] = (checked_at, result)
            logger.info(
                "llm_catalog_checked",
                extra={
                    "provider": backend.name,
                    "models": len(result),
                    "available": sum(
                        a.state is AvailabilityState.AVAILABLE for a in result.values()
                    ),
                    "unavailable": sum(
                        a.state is AvailabilityState.UNAVAILABLE for a in result.values()
                    ),
                },
            )
            return result

    async def catalog(self, refresh: bool = False) -> tuple[list[ProviderInfo], list[ModelInfo]]:
        backends = list(self.backends.values())
        checks = await asyncio.gather(*(self._availability(b, refresh) for b in backends))
        providers: list[ProviderInfo] = []
        models: list[ModelInfo] = []
        for backend, availability in zip(backends, checks, strict=True):
            providers.append(
                ProviderInfo(
                    name=backend.name,
                    label=backend.label,
                    cost=backend.cost,
                    configured=backend.configured,
                    reason=None if backend.configured else backend.not_configured_reason,
                    notice=backend.notice,
                )
            )
            for model in backend.models:
                if backend.configured:
                    state = availability.get(model, Availability.unknown())
                else:
                    state = Availability.unavailable(
                        backend.not_configured_reason or "The provider is not configured."
                    )
                models.append(
                    ModelInfo(
                        provider=backend.name,
                        provider_label=backend.label,
                        model=model,
                        cost=backend.cost,
                        configured=backend.configured,
                        availability=state,
                        default=self.default == ModelRef(backend.name, model),
                    )
                )
        return providers, models

    async def aclose(self) -> None:
        for backend in self.backends.values():
            try:
                await backend.aclose()
            except Exception:
                logger.exception("llm_backend_close_failed", extra={"provider": backend.name})
