"""Language model providers for chat answers."""

from app.core.config import Settings
from app.llm.base import LLMProvider

__all__ = ["LLMProvider", "create_llm_provider"]


def create_llm_provider(settings: Settings) -> LLMProvider | None:
    """Build the configured provider, or None when no credentials are configured.

    Without credentials the API still starts; chat requests answer 503 llm_not_configured.
    There is no stand-in provider that would produce made-up answers.
    """
    if not settings.llm_configured:
        return None
    from app.llm.anthropic import AnthropicProvider

    return AnthropicProvider(settings)
