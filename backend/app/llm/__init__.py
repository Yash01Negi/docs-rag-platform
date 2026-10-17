"""Language model providers for chat answers."""

from app.core.config import Settings
from app.llm.base import LLMProvider
from app.llm.catalog import Cost, ModelRef
from app.llm.registry import LLMRegistry, SelectionError

__all__ = [
    "Cost",
    "LLMProvider",
    "LLMRegistry",
    "ModelRef",
    "SelectionError",
    "create_llm_provider",
    "create_llm_registry",
]


def create_llm_registry(settings: Settings) -> LLMRegistry:
    """The registry of every configured provider and model (Anthropic, Gemini, Ollama)."""
    return LLMRegistry.from_settings(settings)


def create_llm_provider(settings: Settings) -> LLMProvider | None:
    """The provider of the default model, or None when it is not configured.

    Without credentials the API still starts; chat requests answer 503 llm_not_configured.
    There is no stand-in provider that would produce made-up answers.
    """
    model = settings.default_model
    if model is None:
        return None
    if settings.llm_provider == "anthropic":
        if not settings.llm_configured:
            return None
        from app.llm.anthropic import AnthropicProvider

        return AnthropicProvider(settings, model=model)
    if settings.llm_provider == "gemini":
        if not settings.gemini_configured:
            return None
        from app.llm.gemini import GeminiProvider

        return GeminiProvider(settings, model=model)
    if not settings.ollama_configured:
        return None
    from app.llm.ollama import OllamaProvider

    return OllamaProvider(settings, model=model)
