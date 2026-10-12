"""Bilingual (Japanese/English) text analysis for keyword search.

The same analyzer runs at indexing time (in the worker) and at query time (in the API), so
documents and queries always produce comparable terms. It is deliberately implemented in the
application rather than in PostgreSQL: PostgreSQL's built-in text search parsers assume
words are separated by spaces, which is not true for Japanese. See ADR 0005.

How text becomes terms:

1. SudachiPy (with the ``core`` dictionary) segments the text in split mode C (the longest
   dictionary units, such as ``東京都庁`` or ``検索エンジン``). Sudachi also normalises
   width and spelling variants (``ｶﾞｲﾄﾞ`` → ``ガイド``, ``サーバ`` → ``サーバー``,
   ``附属`` → ``付属``).
2. Japanese words are indexed by their normalised form. A compound word is *also* indexed as
   its split-mode-A parts (``東京`` + ``都庁``) at the same positions, so a search for a part
   of a compound still matches (the "search mode" used by Japanese search engines).
3. Particles, auxiliary verbs, punctuation and symbols (``は``, ``を``, ``です``, ``。``)
   produce no term, and a few very common verbs and formal nouns are treated as stop words.
4. Latin-script words (English, product names, identifiers) are lower-cased after Unicode
   NFKC normalisation (``ＰｏｓｔｇｒｅＳＱＬ`` → ``postgresql``), English stop words are
   dropped, and the rest are stemmed with the Snowball English stemmer
   (``documents`` → ``document``). Sudachi's own normal form is not used for them because it
   maps English words to katakana readings.
5. Every token keeps its position, so phrase queries can require words to be adjacent, and
   its character offsets in the original text, so matches can be highlighted.
"""

import re
import threading
import unicodedata
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from functools import lru_cache

import snowballstemmer
from sudachipy import Dictionary, SplitMode

from app.ingestion.language import is_cjk

# Bump when the analysis rules change: stored search vectors must then be rebuilt with
# ``python -m app.worker rebuild-search-index``.
ANALYZER_VERSION = "sudachi-core-c+a.snowball-en.v1"

# PostgreSQL tsvector limits: positions are 1..16383 and a lexeme is at most 2046 bytes.
MAX_POSITION = 16383
MAX_TERM_LENGTH = 64

# Parts of speech that carry no searchable meaning on their own.
_SKIPPED_POS = frozenset({"助詞", "助動詞", "補助記号", "記号", "空白"})
# Normalised forms of light verbs and formal nouns that occur in almost every sentence.
_JAPANESE_STOP_WORDS = frozenset({"為る", "有る", "居る", "成る", "出来る", "事", "物", "為"})
# Lucene's English stop word set, the usual minimal list for search.
# fmt: off
_ENGLISH_STOP_WORDS = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "but", "by", "for", "if", "in", "into", "is",
    "it", "no", "not", "of", "on", "or", "such", "that", "the", "their", "then", "there",
    "these", "they", "this", "to", "was", "will", "with",
})
# fmt: on
_LATIN_WORD = re.compile(r"[0-9a-z]+(?:'[a-z]+)*")
_PHRASE = re.compile(r'"([^"]+)"|“([^”]+)”|「([^」]+)」')


@dataclass(frozen=True)
class Token:
    """A searchable term occurrence. ``start``/``end`` are offsets in the analysed text."""

    term: str
    position: int
    start: int
    end: int
    # True for the whole-compound term indexed alongside its parts (e.g. 東京都庁 next to
    # 東京 and 都庁). Phrase matching uses the parts only.
    compound: bool = False


@dataclass(frozen=True)
class QueryAnalysis:
    terms: tuple[str, ...]
    # Each phrase is a sequence of (position offset, term) pairs from a quoted part of the
    # query; a result must contain every phrase.
    phrases: tuple[tuple[tuple[int, str], ...], ...]

    @property
    def is_empty(self) -> bool:
        return not self.terms


_local = threading.local()


@lru_cache(maxsize=1)
def _dictionary() -> Dictionary:
    return Dictionary(dict="core")


def _tokenizer():  # type: ignore[no-untyped-def]
    # Sudachi tokenizers are not safe to share between threads; the dictionary is.
    tokenizer = getattr(_local, "tokenizer", None)
    if tokenizer is None:
        tokenizer = _dictionary().tokenizer()
        _local.tokenizer = tokenizer
    return tokenizer


@lru_cache(maxsize=1)
def _stemmer() -> snowballstemmer.stemmer:
    return snowballstemmer.stemmer("english")


_stem_lock = threading.Lock()


def _stem(word: str) -> str:
    with _stem_lock:  # the pure-Python stemmer keeps internal state
        return _stemmer().stemWord(word)


def _normalise(text: str) -> str:
    return unicodedata.normalize("NFKC", text).casefold()


def _is_latin(surface: str) -> bool:
    normalised = _normalise(surface)
    return any(char.isascii() and char.isalnum() for char in normalised) and not any(
        is_cjk(char) for char in normalised
    )


def warm_up() -> None:
    """Load the dictionary and stemmer now rather than on the first request."""
    analyze("YashRAG 検索の準備")


def analyze(text: str) -> list[Token]:
    """Split ``text`` into searchable tokens (see the module docstring)."""
    if not text.strip():
        return []
    tokens: list[Token] = []
    position = 1
    for morpheme in _tokenizer().tokenize(text, SplitMode.C):
        pos = morpheme.part_of_speech()[0]
        if pos == "空白":
            continue
        surface = morpheme.surface()
        start = morpheme.begin()
        if _is_latin(surface):
            for match in _LATIN_WORD.finditer(_normalise(surface)):
                word = match.group().replace("'", "")
                if word not in _ENGLISH_STOP_WORDS and len(word) <= MAX_TERM_LENGTH:
                    term = _stem(word) if word.isalpha() else word
                    # NFKC can change lengths, so latin offsets cover the whole morpheme.
                    tokens.append(Token(term, position, start, morpheme.end()))
                position += 1
            continue
        if pos in _SKIPPED_POS:
            position += 1
            continue
        parts = morpheme.split(SplitMode.A)
        whole = _normalise(morpheme.normalized_form())
        if whole in _JAPANESE_STOP_WORDS or len(whole) > MAX_TERM_LENGTH:
            position += max(1, len(parts))
            continue
        if len(parts) > 1:
            tokens.append(Token(whole, position, start, morpheme.end(), compound=True))
            for part in parts:
                term = _normalise(part.normalized_form())
                if term not in _JAPANESE_STOP_WORDS:
                    tokens.append(Token(term, position, part.begin(), part.end()))
                position += 1
        else:
            tokens.append(Token(whole, position, start, morpheme.end()))
            position += 1
    return [token for token in tokens if token.position <= MAX_POSITION and token.term]


def _quote(term: str) -> str:
    # tsvector/tsquery input syntax: quote the lexeme, doubling quotes and backslashes.
    return "'" + term.replace("\\", "\\\\").replace("'", "''") + "'"


def to_tsvector_literal(tokens: Iterable[Token]) -> str:
    """The tokens as a PostgreSQL ``tsvector`` literal, e.g. ``'検索':1 'document':2,5``.

    The literal is passed as a bound parameter and cast with ``CAST(:v AS tsvector)``, so
    PostgreSQL stores exactly these lexemes without re-parsing them.
    """
    positions: dict[str, set[int]] = {}
    for token in tokens:
        positions.setdefault(token.term, set()).add(token.position)
    return " ".join(
        f"{_quote(term)}:{','.join(str(p) for p in sorted(found))}"
        for term, found in sorted(positions.items())
    )


def analyze_query(query: str) -> QueryAnalysis:
    """Terms of a search query, plus phrases written in ``"..."``, ``“...”`` or ``「...」``."""
    terms: list[str] = []
    phrases: list[tuple[tuple[int, str], ...]] = []
    for token in analyze(query):
        if token.term not in terms:
            terms.append(token.term)
    for match in _PHRASE.finditer(query):
        phrase_text = next(group for group in match.groups() if group is not None)
        parts = [token for token in analyze(phrase_text) if not token.compound]
        if parts:
            first = parts[0].position
            phrase = tuple((token.position - first, token.term) for token in parts)
            if phrase not in phrases:
                phrases.append(phrase)
    return QueryAnalysis(terms=tuple(terms), phrases=tuple(phrases))


def term_query(term: str) -> str:
    """A ``tsquery`` literal matching one term exactly."""
    return _quote(term)


def any_term_query(terms: Sequence[str]) -> str:
    """A ``tsquery`` literal matching any of the terms."""
    return " | ".join(_quote(term) for term in terms)


def phrase_query(phrase: Sequence[tuple[int, str]]) -> str:
    """A ``tsquery`` literal matching the phrase's terms at their relative positions."""
    parts = [_quote(phrase[0][1])]
    for (previous, _), (offset, term) in zip(phrase, phrase[1:], strict=False):
        parts.append(f"<{offset - previous}> {_quote(term)}")
    return " ".join(parts)


def highlight_spans(text: str, terms: Iterable[str]) -> list[tuple[int, int]]:
    """Character ranges in ``text`` whose tokens match one of ``terms``, merged and sorted."""
    wanted = set(terms)
    spans = [(token.start, token.end) for token in analyze(text) if token.term in wanted]
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged
