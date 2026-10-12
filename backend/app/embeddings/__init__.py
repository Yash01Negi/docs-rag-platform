"""Embedding providers."""

from app.core.config import Settings
from app.embeddings.base import EmbeddingModelInfo, EmbeddingProvider

__all__ = ["EmbeddingModelInfo", "EmbeddingProvider", "create_embedding_provider"]


def create_embedding_provider(settings: Settings) -> EmbeddingProvider:
    """Build the configured provider. Heavy model libraries are imported lazily."""
    if settings.embedding_provider == "fake":
        from app.embeddings.fake import FakeEmbeddingProvider

        return FakeEmbeddingProvider(
            settings.embedding_dimension,
            settings.embedding_batch_size,
            max_input_tokens=settings.embedding_max_seq_length - 2,
        )

    from app.embeddings.bge_m3 import BgeM3EmbeddingProvider

    return BgeM3EmbeddingProvider(
        model_name=settings.embedding_model_name,
        revision=settings.embedding_model_revision,
        dimension=settings.embedding_dimension,
        batch_size=settings.embedding_batch_size,
        device=settings.embedding_device,
        max_seq_length=settings.embedding_max_seq_length,
        cache_dir=settings.model_cache_dir,
        local_files_only=settings.embedding_local_files_only,
    )
