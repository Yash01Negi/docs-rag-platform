"""Sentence-aware chunking for Japanese, English and mixed text.

Pipeline:

1. Each extracted block (paragraph or heading) is split into sentence units. Sentence ends
   are Japanese ``。！？`` (and full-width ``．``), English ``.!?`` followed by whitespace,
   and line breaks inside a block. Japanese text is never split on whitespace, because
   whitespace does not separate Japanese words.
2. A unit longer than the chunk target is split further at clause punctuation
   (``、，,;；:：``) or whitespace, and as a last resort at the longest character prefix that
   fits. Nothing is dropped or truncated.
3. Units are packed greedily into chunks of at most ``target_tokens`` tokens, measured on the
   exact chunk text with the embedding model's tokenizer.
4. Each new chunk repeats whole sentences from the end of the previous chunk, up to
   ``overlap_tokens`` tokens. If the last sentence alone is longer than the overlap budget,
   that boundary has no overlap (sentences are never cut to create overlap).
5. A heading starts a new chunk (without overlap) once the current chunk has at least
   ``min_tokens`` tokens of new content, so chunks tend to stay within one section.
6. If the final chunk has fewer than ``min_tokens`` tokens of new content, it is merged into
   the previous chunk when the result stays within ``target_tokens + min_tokens``.

Every chunk records its page range (PDF), section heading and detected language.
"""

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from app.db.models import Language
from app.ingestion.extraction import Block
from app.ingestion.language import detect_language, is_cjk

TokenCounter = Callable[[str], int]

_JAPANESE_SENTENCE_END = frozenset("。！？．｡")
_LATIN_SENTENCE_END = frozenset(".!?")
_CLOSERS = frozenset("」』）)]】〕\"'”’")
_CLAUSE_BREAK = re.compile(r"(?<=[、，,;；:：])|(?<=\s)(?=\S)")
_PARAGRAPH_SEPARATOR = "\n\n"


@dataclass(frozen=True)
class ChunkingConfig:
    target_tokens: int = 400
    overlap_tokens: int = 60
    min_tokens: int = 40

    def __post_init__(self) -> None:
        if self.target_tokens < 1:
            raise ValueError("target_tokens must be positive")
        if not 0 <= self.overlap_tokens < self.target_tokens:
            raise ValueError("overlap_tokens must be in [0, target_tokens)")
        if not 0 <= self.min_tokens < self.target_tokens:
            raise ValueError("min_tokens must be in [0, target_tokens)")

    @property
    def max_tokens(self) -> int:
        return self.target_tokens + self.min_tokens


@dataclass(frozen=True)
class TextChunk:
    ordinal: int
    text: str
    token_count: int
    language: Language
    page_start: int | None
    page_end: int | None
    section: str | None


@dataclass(frozen=True)
class _Unit:
    text: str
    tokens: int
    page: int | None
    section: str | None
    block_index: int
    # Separator placed before this unit when the previous unit is from the same block.
    joiner: str
    is_heading: bool


def split_sentences(text: str) -> list[tuple[str, str]]:
    """Split text into ``(separator_before, sentence)`` pairs.

    The separator records what stood between two sentences in the source (a newline, a
    space, or nothing for Japanese), so chunks can be re-assembled faithfully.
    """
    sentences: list[tuple[str, str]] = []
    start = 0
    pending_separator = ""
    length = len(text)
    index = 0

    def emit(end: int) -> None:
        nonlocal pending_separator
        piece = text[start:end].strip(" \t")
        if piece.strip():
            sentences.append((pending_separator if sentences else "", piece))

    while index < length:
        char = text[index]
        if char == "\n":
            emit(index)
            index += 1
            while index < length and text[index] in " \t\n":
                index += 1
            start = index
            pending_separator = "\n"
            continue
        is_end = char in _JAPANESE_SENTENCE_END or (
            char in _LATIN_SENTENCE_END and (index + 1 == length or text[index + 1] in " \t\n")
        )
        if char in "!?" and index + 1 < length and is_cjk(text[index + 1]):
            is_end = True  # "!" or "?" directly followed by Japanese text
        if is_end:
            index += 1
            while index < length and text[index] in _CLOSERS:
                index += 1
            emit(index)
            separator = ""
            while index < length and text[index] in " \t":
                separator = " "
                index += 1
            start = index
            pending_separator = separator
            continue
        index += 1
    emit(length)
    return sentences


def _units_text(units: Sequence[_Unit]) -> str:
    text = ""
    previous: _Unit | None = None
    for unit in units:
        if previous is None:
            text = unit.text
        elif previous.block_index == unit.block_index:
            text = f"{text}{unit.joiner}{unit.text}"
        else:
            text = f"{text}{_PARAGRAPH_SEPARATOR}{unit.text}"
        previous = unit
    return text


class Chunker:
    def __init__(self, count_tokens: TokenCounter, config: ChunkingConfig) -> None:
        self._count = count_tokens
        self.config = config

    # -- splitting -------------------------------------------------------------------------

    def _split_long(self, text: str) -> list[tuple[str, str]]:
        """Split an over-long sentence into ``(separator, piece)`` pairs within the target.

        Pieces end at clause punctuation or whitespace where possible; a run with no such
        break (a long URL, or unpunctuated Japanese) is cut at the longest prefix that fits.
        Separators record the original spacing, so joining the pieces restores the text.
        """
        budget = self.config.target_tokens
        raw_pieces: list[str] = []
        current = ""
        for part in (p for p in _CLAUSE_BREAK.split(text) if p):
            if self._count((current + part).strip()) <= budget:
                current += part
                continue
            if current:
                raw_pieces.append(current)
            current = part
            while self._count(current.strip()) > budget:
                cut = self._longest_fitting_prefix(current, budget)
                raw_pieces.append(current[:cut])
                current = current[cut:]
        if current:
            raw_pieces.append(current)

        result: list[tuple[str, str]] = []
        previous_raw = ""
        for raw in raw_pieces:
            piece = raw.strip()
            if piece:
                spaced = previous_raw[-1:].isspace() or raw[:1].isspace()
                result.append((" " if spaced and result else "", piece))
            previous_raw = raw
        return result

    def _longest_fitting_prefix(self, text: str, budget: int) -> int:
        low, high = 1, len(text)
        while low < high:
            mid = (low + high + 1) // 2
            if self._count(text[:mid].strip()) <= budget:
                low = mid
            else:
                high = mid - 1
        return low  # at least one character, so progress is always made

    def _units(self, blocks: Sequence[Block]) -> list[_Unit]:
        units: list[_Unit] = []
        for block_index, block in enumerate(blocks):
            if block.is_heading:
                sentences = [("", block.text.strip())]
            else:
                sentences = split_sentences(block.text)
            for separator, sentence in sentences:
                if self._count(sentence) <= self.config.target_tokens:
                    pieces = [(separator, sentence)]
                else:
                    pieces = self._split_long(sentence)
                    pieces[0] = (separator, pieces[0][1])
                for piece_separator, piece in pieces:
                    tokens = self._count(piece)
                    if tokens == 0:
                        continue
                    units.append(
                        _Unit(
                            text=piece,
                            tokens=tokens,
                            page=block.page,
                            section=block.section,
                            block_index=block_index,
                            joiner=piece_separator,
                            is_heading=block.is_heading,
                        )
                    )
        return units

    # -- packing ---------------------------------------------------------------------------

    def _overlap_tail(self, units: Sequence[_Unit]) -> list[_Unit]:
        tail: list[_Unit] = []
        for unit in reversed(units):
            candidate = [unit, *tail]
            if self._count(_units_text(candidate)) > self.config.overlap_tokens:
                break
            tail = candidate
        return tail

    def chunk(self, blocks: Sequence[Block]) -> list[TextChunk]:
        units = self._units(blocks)
        target = self.config.target_tokens
        groups: list[tuple[list[_Unit], int]] = []  # (units, index of first new unit)
        current: list[_Unit] = []
        new_start = 0

        def new_tokens() -> int:
            return self._count(_units_text(current[new_start:])) if current[new_start:] else 0

        for unit in units:
            if not current:
                current, new_start = [unit], 0
                continue
            if unit.is_heading and new_tokens() >= self.config.min_tokens:
                groups.append((current, new_start))
                current, new_start = [unit], 0
                continue
            candidate = [*current, unit]
            if self._count(_units_text(candidate)) <= target:
                current = candidate
                continue
            groups.append((current, new_start))
            overlap = self._overlap_tail(current)
            while overlap and self._count(_units_text([*overlap, unit])) > target:
                overlap = overlap[1:]
            current, new_start = [*overlap, unit], len(overlap)
        if current:
            groups.append((current, new_start))

        groups = self._merge_small_tail(groups)
        return [self._build(ordinal, group, start) for ordinal, (group, start) in enumerate(groups)]

    def _merge_small_tail(
        self, groups: list[tuple[list[_Unit], int]]
    ) -> list[tuple[list[_Unit], int]]:
        if len(groups) < 2:
            return groups
        last_units, last_start = groups[-1]
        new_part = last_units[last_start:]
        if self._count(_units_text(new_part)) >= self.config.min_tokens:
            return groups
        previous_units, previous_start = groups[-2]
        merged = [*previous_units, *new_part]
        if self._count(_units_text(merged)) > self.config.max_tokens:
            return groups
        return [*groups[:-2], (merged, previous_start)]

    def _build(self, ordinal: int, units: list[_Unit], new_start: int) -> TextChunk:
        text = _units_text(units)
        pages = [unit.page for unit in units if unit.page is not None]
        first_new = units[new_start] if new_start < len(units) else units[0]
        return TextChunk(
            ordinal=ordinal,
            text=text,
            token_count=self._count(text),
            language=detect_language(text),
            page_start=min(pages) if pages else None,
            page_end=max(pages) if pages else None,
            section=first_new.section,
        )


def chunk_blocks(
    blocks: Sequence[Block], count_tokens: TokenCounter, config: ChunkingConfig
) -> list[TextChunk]:
    return Chunker(count_tokens, config).chunk(blocks)
