"""Source selection and context budgeting.

Retrieved hits arrive in rank order. Sources are taken in that order, skipping exact
duplicates (the same text uploaded twice, compared after Unicode and whitespace
normalisation), until the estimated token budget is spent. A passage that does not fit is
skipped so that a smaller later one can still be used; only the first passage is ever
shortened, so a single very long chunk cannot leave the model with nothing.

History is budgeted separately: the most recent complete question/answer pairs that fit.
"""

import hashlib
import unicodedata
from dataclasses import dataclass

from app.chat.citations import strip_markers
from app.chat.prompt import PromptSource, format_source
from app.chat.tokens import estimate_tokens, truncate_to_tokens
from app.search.service import SearchHit


@dataclass(frozen=True)
class SelectedSource:
    index: int  # 1-based; the N in [S<N>]
    hit: SearchHit
    text: str  # the text given to the model (the chunk text, possibly shortened)
    truncated: bool

    def prompt_source(self) -> PromptSource:
        chunk = self.hit.chunk
        return PromptSource(
            index=self.index,
            filename=chunk.filename,
            text=self.text,
            language=chunk.language,
            page_start=chunk.page_start,
            page_end=chunk.page_end,
            section=chunk.section,
        )


def text_fingerprint(text: str) -> str:
    normalized = " ".join(unicodedata.normalize("NFKC", text).split()).casefold()
    return hashlib.sha256(normalized.encode()).hexdigest()


def chunk_text_sha256(text: str) -> str:
    """Hash of the exact chunk text, matching PostgreSQL's sha256(convert_to(text, 'UTF8'))."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def select_sources(hits: list[SearchHit], token_budget: int) -> list[SelectedSource]:
    selected: list[SelectedSource] = []
    seen: set[str] = set()
    remaining = token_budget
    for hit in hits:
        fingerprint = text_fingerprint(hit.chunk.text)
        if fingerprint in seen:
            continue
        candidate = SelectedSource(len(selected) + 1, hit, hit.chunk.text, truncated=False)
        cost = estimate_tokens(format_source(candidate.prompt_source()))
        if cost > remaining:
            if selected:
                continue
            overhead = cost - estimate_tokens(hit.chunk.text)
            text = truncate_to_tokens(hit.chunk.text, max(remaining - overhead, 0))
            if not text.strip():
                continue
            candidate = SelectedSource(1, hit, text, truncated=True)
            cost = estimate_tokens(format_source(candidate.prompt_source()))
        seen.add(fingerprint)
        selected.append(candidate)
        remaining -= cost
    return selected


def select_history(
    pairs: list[tuple[str, str]], token_budget: int, max_messages: int
) -> list[tuple[str, str]]:
    """The most recent (question, answer) pairs that fit, oldest first; markers removed."""
    chosen: list[tuple[str, str]] = []
    remaining = token_budget
    for question, answer in reversed(pairs):
        if (len(chosen) + 1) * 2 > max_messages:
            break
        cleaned = strip_markers(answer)
        if not cleaned:
            continue
        cost = estimate_tokens(question) + estimate_tokens(cleaned)
        if cost > remaining:
            break
        chosen.append((question, cleaned))
        remaining -= cost
    chosen.reverse()
    return chosen
