"""The model catalog: which provider/model pairs this server offers, and their state.

The catalog is built from configuration only (``ANTHROPIC_MODELS``, ``GEMINI_MODELS``,
``OLLAMA_MODELS``). A browser chooses one of its entries by provider name and model ID; a
pair that is not in the catalog is rejected, so a client can never make the server call an
arbitrary model, provider or URL.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Cost(StrEnum):
    """How using a model is paid for, as shown in the model selector."""

    PAID = "paid"  # billed per token to the server's account
    FREE_TIER = "free_tier"  # a provider's free tier: rate-limited, data may be used by it
    LOCAL = "local"  # runs on the configured Ollama server (normally the same machine)


class AvailabilityState(StrEnum):
    AVAILABLE = "available"  # the provider confirmed the model can be used
    UNAVAILABLE = "unavailable"  # the provider said no (unknown model, not installed, ...)
    UNKNOWN = "unknown"  # not checked yet, or the check itself failed


@dataclass(frozen=True)
class Availability:
    state: AvailabilityState
    # Safe to show to users: never a provider's raw error text, a URL or a credential.
    reason: str | None = None
    # True when the state may change any moment (the provider could not be reached): such
    # results are re-checked sooner.
    transient: bool = False

    @classmethod
    def available(cls) -> Availability:
        return cls(AvailabilityState.AVAILABLE)

    @classmethod
    def unavailable(cls, reason: str, *, transient: bool = False) -> Availability:
        return cls(AvailabilityState.UNAVAILABLE, reason, transient)

    @classmethod
    def unknown(cls, reason: str | None = None) -> Availability:
        return cls(AvailabilityState.UNKNOWN, reason, True)


@dataclass(frozen=True)
class ModelRef:
    """A provider/model pair, as named in a chat request."""

    provider: str
    model: str


@dataclass(frozen=True)
class ModelInfo:
    provider: str
    provider_label: str
    model: str
    cost: Cost
    configured: bool
    availability: Availability
    default: bool

    @property
    def selectable(self) -> bool:
        return self.configured and self.availability.state is not AvailabilityState.UNAVAILABLE


@dataclass(frozen=True)
class ProviderInfo:
    name: str
    label: str
    cost: Cost
    configured: bool
    # Why the provider cannot be used (missing key, no base URL); safe to show.
    reason: str | None
    # Shown next to the provider's models: rate limits, data use, local-only.
    notice: str | None
