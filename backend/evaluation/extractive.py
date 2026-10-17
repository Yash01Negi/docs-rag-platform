"""A non-LLM extractive baseline that plugs into the chat pipeline for evaluation.

It answers by copying the source sentence(s) that share the most terms with the question,
with a citation marker, and abstains with ``[NOT_FOUND]`` when no sentence shares enough.
It cannot translate, paraphrase or combine facts, so it shows what retrieval plus copying
achieves; it says nothing about the quality of a language model. It exists so the answer
evaluation harness can run end to end, deterministically, without an API key (and in CI).
It is never used by the application.
"""

import re
import unicodedata
from collections.abc import AsyncIterator
from dataclasses import dataclass

from app.chat.citations import ABSTAIN_MARKER
from app.ingestion.language import is_cjk
from app.llm.base import (
    GenerationDone,
    GenerationEvent,
    GenerationRequest,
    LLMProvider,
    StopReason,
    TextDelta,
    Usage,
)

_SOURCE = re.compile(r'<source id="S(\d+)"[^>]*>\n(.*?)\n</source>', re.DOTALL)
_QUESTION = re.compile(r"<question>\n(.*?)\n</question>", re.DOTALL)
_SENTENCE_END = re.compile(r"(?<=[。．！？!?])|(?<=\.)\s+|\n+")
_WORD = re.compile(r"[a-z0-9][a-z0-9'-]*")
_STOPWORD_TEXT = (
    "a an and are as at be by can do does for from how in is "
    "it many much of on or the to under what when which who why with ones"
)
_STOPWORDS = frozenset(_STOPWORD_TEXT.split())
MIN_SCORE = 0.35
CHUNK_CHARS = 24

NOT_FOUND_TEXT = {
    "ja": "アップロードされた文書からは、この質問に回答できません。",
    "en": "The uploaded documents do not answer this question.",
}


def terms(text: str) -> set[str]:
    """Latin words (without stopwords) and Japanese character bigrams."""
    text = unicodedata.normalize("NFKC", text).casefold()
    found = {w for w in _WORD.findall(text) if len(w) > 1 and w not in _STOPWORDS}
    run: list[str] = []
    for char in [*text, " "]:
        if (is_cjk(char) and char.isalpha()) or "぀" <= char <= "ヿ":
            run.append(char)
            continue
        found.update("".join(run[i : i + 2]) for i in range(len(run) - 1))
        if len(run) == 1:
            found.add(run[0])
        run = []
    return found


def sentences(text: str) -> list[str]:
    return [s.strip(" #\t") for s in _SENTENCE_END.split(text) if s and s.strip(" #\t")]


@dataclass(frozen=True)
class Pick:
    score: float
    index: int
    sentence: str


def extract(question: str, sources: list[tuple[int, str]]) -> list[Pick]:
    wanted = terms(question)
    if not wanted:
        return []
    picks = [
        Pick(len(wanted & terms(sentence)) / len(wanted), index, sentence)
        for index, text in sources
        for sentence in sentences(text)
    ]
    picks.sort(key=lambda p: (-p.score, p.index))
    if not picks or picks[0].score < MIN_SCORE:
        return []
    best = picks[0]
    chosen = [best]
    for pick in picks[1:]:
        if pick.score >= max(MIN_SCORE, 0.8 * best.score) and pick.index != best.index:
            chosen.append(pick)
            break
    return chosen


class ExtractiveProvider(LLMProvider):
    name = "extractive-baseline"
    model = "extractive-baseline"

    async def stream(self, request: GenerationRequest) -> AsyncIterator[GenerationEvent]:
        prompt = request.messages[-1].content
        question_match = _QUESTION.search(prompt)
        question = question_match.group(1) if question_match else ""
        sources = [(int(number), text) for number, text in _SOURCE.findall(prompt)]
        picks = extract(question, sources)
        if picks:
            answer = " ".join(f"{pick.sentence} [S{pick.index}]" for pick in picks)
        else:
            language = "ja" if "Japanese" in prompt.rsplit("Answer language:", 1)[-1] else "en"
            answer = f"[{ABSTAIN_MARKER}] {NOT_FOUND_TEXT[language]}"
        for start in range(0, len(answer), CHUNK_CHARS):
            yield TextDelta(answer[start : start + CHUNK_CHARS])
        yield GenerationDone(StopReason.END, Usage(), self.model, raw_stop_reason="end_turn")
