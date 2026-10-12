"""Load and check the evaluation dataset (documents, queries and judgments)."""

import hashlib
import re
import tomllib
import unicodedata
from dataclasses import dataclass
from pathlib import Path

DATASET_DIR = Path(__file__).parent / "dataset"
QUERY_LANGUAGES = ("en", "ja", "mixed")
QUERY_TYPES = (
    "exact_keyword",
    "paraphrase",
    "ja_no_spaces",
    "cross_language",
    "mixed",
    "no_relevant",
    "multi_relevant",
    "distractor",
)
_WHITESPACE = re.compile(r"\s+")


def normalize_for_matching(text: str) -> str:
    """NFKC and collapsed whitespace, so anchors match across chunk line breaks."""
    return _WHITESPACE.sub(" ", unicodedata.normalize("NFKC", text)).strip()


@dataclass(frozen=True)
class Passage:
    doc: str
    anchor: str
    grade: int


@dataclass(frozen=True)
class Query:
    id: str
    text: str
    language: str
    types: tuple[str, ...]
    relevant: tuple[Passage, ...]
    notes: str = ""


@dataclass(frozen=True)
class Chunking:
    target_tokens: int
    overlap_tokens: int
    min_tokens: int


@dataclass(frozen=True)
class Dataset:
    version: int
    documents: dict[str, Path]
    queries: tuple[Query, ...]
    chunking: Chunking
    sha256: str


class DatasetError(ValueError):
    pass


def load_dataset(directory: Path = DATASET_DIR) -> Dataset:
    raw = tomllib.loads((directory / "queries.toml").read_text(encoding="utf-8"))
    documents = {
        path.name: path for path in sorted((directory / "documents").iterdir()) if path.is_file()
    }
    queries = tuple(_query(entry) for entry in raw["queries"])
    chunking = Chunking(**raw["chunking"])
    dataset = Dataset(
        version=raw["version"],
        documents=documents,
        queries=queries,
        chunking=chunking,
        sha256=_digest(directory, documents),
    )
    check_dataset(dataset)
    return dataset


def _query(entry: dict) -> Query:  # type: ignore[type-arg]
    return Query(
        id=entry["id"],
        text=entry["query"],
        language=entry["language"],
        types=tuple(entry["types"]),
        relevant=tuple(Passage(**passage) for passage in entry["relevant"]),
        notes=entry.get("notes", ""),
    )


def _digest(directory: Path, documents: dict[str, Path]) -> str:
    digest = hashlib.sha256()
    for path in [directory / "queries.toml", *documents.values()]:
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def check_dataset(dataset: Dataset) -> None:
    """Fail loudly on judgments that cannot be right, before any search runs."""
    texts = {
        name: normalize_for_matching(path.read_text(encoding="utf-8"))
        for name, path in dataset.documents.items()
    }
    seen: set[str] = set()
    for query in dataset.queries:
        where = f"query {query.id}"
        if query.id in seen:
            raise DatasetError(f"{where}: duplicate id")
        seen.add(query.id)
        if not query.text.strip():
            raise DatasetError(f"{where}: empty query")
        if query.language not in QUERY_LANGUAGES:
            raise DatasetError(f"{where}: unknown language {query.language!r}")
        unknown = set(query.types) - set(QUERY_TYPES)
        if unknown or not query.types:
            raise DatasetError(f"{where}: unknown or missing types {sorted(unknown)}")
        if ("no_relevant" in query.types) != (not query.relevant):
            raise DatasetError(f"{where}: no_relevant queries, and only they, judge nothing")
        if "multi_relevant" in query.types and len({p.doc for p in query.relevant}) < 2:
            raise DatasetError(f"{where}: multi_relevant needs passages in two documents")
        anchors: set[tuple[str, str]] = set()
        for passage in query.relevant:
            if passage.doc not in texts:
                raise DatasetError(f"{where}: unknown document {passage.doc!r}")
            if passage.grade not in (1, 2):
                raise DatasetError(f"{where}: grade must be 1 or 2")
            anchor = normalize_for_matching(passage.anchor)
            if anchor not in texts[passage.doc]:
                raise DatasetError(f"{where}: anchor {passage.anchor!r} not in {passage.doc}")
            if (passage.doc, anchor) in anchors:
                raise DatasetError(f"{where}: passage {passage.anchor!r} is judged twice")
            anchors.add((passage.doc, anchor))
    missing = set(QUERY_TYPES) - {t for q in dataset.queries for t in q.types}
    if missing:
        raise DatasetError(f"no query covers the types {sorted(missing)}")
