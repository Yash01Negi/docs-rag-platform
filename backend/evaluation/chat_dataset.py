"""Load and check the answer evaluation set (dataset/chat_qa.toml)."""

import hashlib
import tomllib
from dataclasses import dataclass
from pathlib import Path

from evaluation.dataset import DATASET_DIR, DatasetError, normalize_for_matching

ANSWER_LANGUAGES = ("en", "ja")
QUESTION_TYPES = (
    "en_en",
    "ja_ja",
    "en_to_ja",
    "ja_to_en",
    "mixed",
    "multi_source",
    "follow_up",
    "distractor",
    "no_answer",
)


@dataclass(frozen=True)
class Evidence:
    doc: str
    anchor: str


@dataclass(frozen=True)
class ChatQuestion:
    id: str
    question: str
    answer_language: str
    types: tuple[str, ...]
    evidence: tuple[Evidence, ...]
    # Each fact is a tuple of accepted spellings.
    facts: tuple[tuple[str, ...], ...]
    abstain: bool = False
    follow_up_of: str | None = None
    notes: str = ""


@dataclass(frozen=True)
class ChatDataset:
    version: int
    documents: dict[str, Path]
    questions: tuple[ChatQuestion, ...]
    sha256: str


def load_chat_dataset(directory: Path = DATASET_DIR) -> ChatDataset:
    path = directory / "chat_qa.toml"
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    documents = {p.name: p for p in sorted((directory / "documents").iterdir()) if p.is_file()}
    digest = hashlib.sha256()
    for item in [path, *documents.values()]:
        digest.update(item.name.encode())
        digest.update(item.read_bytes())
    dataset = ChatDataset(
        version=raw["version"],
        documents=documents,
        questions=tuple(_question(entry) for entry in raw["questions"]),
        sha256=digest.hexdigest(),
    )
    check_chat_dataset(dataset)
    return dataset


def _question(entry: dict) -> ChatQuestion:  # type: ignore[type-arg]
    return ChatQuestion(
        id=entry["id"],
        question=entry["question"],
        answer_language=entry["answer_language"],
        types=tuple(entry["types"]),
        evidence=tuple(Evidence(**item) for item in entry.get("evidence", [])),
        facts=tuple(tuple(spellings) for spellings in entry.get("facts", [])),
        abstain=entry.get("abstain", False),
        follow_up_of=entry.get("follow_up_of"),
        notes=entry.get("notes", ""),
    )


def check_chat_dataset(dataset: ChatDataset) -> None:
    """Fail on judgments that cannot be right, before anything is run."""
    texts = {
        name: normalize_for_matching(path.read_text(encoding="utf-8"))
        for name, path in dataset.documents.items()
    }
    seen: list[str] = []
    for question in dataset.questions:
        where = f"question {question.id}"
        if question.id in seen:
            raise DatasetError(f"{where}: duplicate id")
        if not question.question.strip():
            raise DatasetError(f"{where}: empty question")
        if question.answer_language not in ANSWER_LANGUAGES:
            raise DatasetError(f"{where}: unknown answer language {question.answer_language!r}")
        unknown = set(question.types) - set(QUESTION_TYPES)
        if unknown or not question.types:
            raise DatasetError(f"{where}: unknown or missing types {sorted(unknown)}")
        if question.abstain != ("no_answer" in question.types):
            raise DatasetError(f"{where}: abstain must be set exactly for no_answer questions")
        if question.abstain and (question.evidence or question.facts):
            raise DatasetError(f"{where}: a no_answer question has no evidence or facts")
        if not question.abstain and (not question.evidence or not question.facts):
            raise DatasetError(f"{where}: an answerable question needs evidence and facts")
        if any(
            not spellings or not all(s.strip() for s in spellings) for spellings in question.facts
        ):
            raise DatasetError(f"{where}: every fact needs at least one non-empty spelling")
        if "multi_source" in question.types and len({e.doc for e in question.evidence}) < 2:
            raise DatasetError(f"{where}: multi_source needs evidence in two documents")
        if ("follow_up" in question.types) != (question.follow_up_of is not None):
            raise DatasetError(f"{where}: follow_up questions, and only they, name follow_up_of")
        if question.follow_up_of is not None and (not seen or seen[-1] != question.follow_up_of):
            raise DatasetError(f"{where}: must come right after {question.follow_up_of}")
        for evidence in question.evidence:
            if evidence.doc not in texts:
                raise DatasetError(f"{where}: unknown document {evidence.doc!r}")
            if normalize_for_matching(evidence.anchor) not in texts[evidence.doc]:
                raise DatasetError(f"{where}: anchor {evidence.anchor!r} not in {evidence.doc}")
        seen.append(question.id)
    missing = set(QUESTION_TYPES) - {t for q in dataset.questions for t in q.types}
    if missing:
        raise DatasetError(f"no question covers the types {sorted(missing)}")
