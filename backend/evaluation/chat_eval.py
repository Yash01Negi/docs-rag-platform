"""Evaluate grounded answers end to end: retrieval, answer, citations, abstention, cost.

    # Deterministic, no API key: the extractive baseline (not a language model)
    uv run python -m evaluation.chat_eval --reset --provider extractive --mode keyword

    # The configured model (needs ANTHROPIC_API_KEY; costs money)
    uv run --extra embeddings python -m evaluation.chat_eval --reset --provider configured

Every question in dataset/chat_qa.toml goes through the real ChatService (history,
retrieval, source selection, prompt, streaming, citation checks, persistence), in a
throwaway database loaded with the evaluation corpus. Follow-up questions are asked in the
same conversation as the question before them. The scores are lexical and deterministic;
there is no model-based judge, so nothing here is a model's opinion of an answer:

* evidence recall: judged evidence passages present in the sources given to the model
* fact recall / correct: required facts present in the answer (accepted spellings)
* citation precision: cited sources that contain judged evidence
* citation coverage: judged evidence passages that the answer cites
* cited sentences: answer sentences that carry at least one citation
* number support (a faithfulness proxy): numbers in the answer that appear in a cited source
* abstention: no_answer questions answered with "not in the documents", and answerable
  questions wrongly answered that way
* answer language, latency (time to first token, total) and tokens

--reset deletes every document and conversation in the target database first, and only
works on a database whose name ends in _eval or _test.
"""

import argparse
import asyncio
import json
import os
import platform
import re
import sys
import tempfile
import time
import unicodedata
import uuid
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncEngine

from app.chat.events import (
    CitationsChecked,
    Delta,
    Failed,
    Finished,
    SourcesFound,
    UsageReported,
)
from app.chat.service import AskRequest, ChatService, RetrievalOptions
from app.core.config import Settings, get_settings
from app.db.models import Chunk, Conversation
from app.db.session import create_engine, create_session_factory
from app.embeddings import create_embedding_provider
from app.ingestion.language import japanese_share
from app.llm import create_llm_provider
from app.llm.base import LLMProvider
from app.search.service import SearchMode, SearchService
from evaluation.chat_dataset import QUESTION_TYPES, ChatDataset, ChatQuestion, load_chat_dataset
from evaluation.dataset import load_dataset, normalize_for_matching
from evaluation.extractive import ExtractiveProvider
from evaluation.metrics import mean, percentile
from evaluation.run import EvaluationError, _git_commit, ingest

ABSTAINED = ("insufficient_evidence", "no_sources")
_DIGIT_GROUP = re.compile(r"(?<=\d),(?=\d{3})")
_SPACE = re.compile(r"\s+")
_NUMBER = re.compile(r"\d+(?:\.\d+)?")
_MARKER = re.compile(r"\[S\d+\]")
_LEADING_MARKERS = re.compile(r"^(?:\[S\d+\]\s*)+")
_SENTENCE = re.compile(r"(?<=[。！？!?])|(?<=[.])\s+|\n+")


# ------------------------------------------------------------------------------- scoring


def normalize_answer(value: str) -> str:
    """NFKC, case folded, no whitespace, no digit-group commas ("3,000 円" -> "3000円")."""
    value = unicodedata.normalize("NFKC", value).casefold()
    return _SPACE.sub("", _DIGIT_GROUP.sub("", value))


def fact_present(answer: str, spellings: Sequence[str]) -> bool:
    normalized = normalize_answer(answer)
    return any(normalize_answer(spelling) in normalized for spelling in spellings)


def numbers_in(value: str) -> list[str]:
    value = _DIGIT_GROUP.sub("", unicodedata.normalize("NFKC", _MARKER.sub("", value)))
    return _NUMBER.findall(value)


def answer_sentences(answer: str) -> list[str]:
    """Sentences of an answer; markers after a sentence's full stop stay with it."""
    sentences: list[str] = []
    for part in (p.strip() for p in _SENTENCE.split(answer)):
        leading = _LEADING_MARKERS.match(part)
        if sentences and leading:
            sentences[-1] += "".join(_MARKER.findall(leading.group()))
            part = part[leading.end() :]
        if len(part) > 1:
            sentences.append(part)
    return sentences


def answer_language(answer: str) -> str | None:
    share = japanese_share(_MARKER.sub("", answer))
    if share is None:
        return None
    return "ja" if share >= 0.5 else "en"


@dataclass
class SourceRecord:
    index: int
    filename: str
    chunk_ordinal: int
    retrieval_rank: int
    evidence: list[int]  # positions in the question's evidence list
    numbers: list[str]


@dataclass
class QuestionRun:
    id: str
    question: str
    types: list[str]
    expected_language: str
    abstain_expected: bool
    status: str
    grounding: str | None
    error_code: str | None
    answer: str
    sources: list[SourceRecord]
    cited: list[int]
    invalid_markers_removed: int
    served_model: str | None
    input_tokens: int | None
    output_tokens: int | None
    first_token_ms: float | None
    total_ms: float | None
    retrieval_ms: float | None
    metrics: dict[str, float | bool | None] = field(default_factory=dict)


def score(question: ChatQuestion, run: QuestionRun) -> dict[str, float | bool | None]:
    """Deterministic, lexical scores for one answer (None = not applicable)."""
    completed = run.status == "complete"
    abstained = completed and run.grounding in ABSTAINED
    language = answer_language(run.answer)
    cited = [s for s in run.sources if s.index in run.cited]
    metrics: dict[str, float | bool | None] = {
        "completed": completed,
        "abstained": abstained,
        "language_ok": None if not run.answer.strip() else language == question.answer_language,
    }
    if question.abstain:
        metrics["abstention_correct"] = abstained
        return metrics
    found_evidence = {e for s in run.sources for e in s.evidence}
    cited_evidence = {e for s in cited for e in s.evidence}
    facts = [fact_present(run.answer, spellings) for spellings in question.facts]
    metrics.update(
        {
            "evidence_recall": len(found_evidence) / len(question.evidence),
            "fact_recall": sum(facts) / len(facts),
            "correct": completed and not abstained and all(facts),
            "false_abstention": abstained,
            "citation_precision": (
                sum(1 for s in cited if s.evidence) / len(cited) if cited else None
            ),
            "citation_coverage": len(cited_evidence) / len(question.evidence),
        }
    )
    if completed and not abstained:
        sentences = answer_sentences(run.answer)
        metrics["cited_sentences"] = (
            sum(1 for s in sentences if _MARKER.search(s)) / len(sentences) if sentences else None
        )
        answer_numbers = numbers_in(run.answer)
        supported = {n for s in cited for n in s.numbers}
        metrics["number_support"] = (
            sum(1 for n in answer_numbers if n in supported) / len(answer_numbers)
            if answer_numbers
            else None
        )
    return metrics


# --------------------------------------------------------------------------------- run


async def reset_chat_database(engine: AsyncEngine) -> None:
    database = make_url(str(engine.url)).database or ""
    if not database.endswith(("_eval", "_test")):
        raise EvaluationError(
            f"refusing to delete data from database {database!r}: "
            "--reset only works on a database whose name ends in _eval or _test"
        )
    async with engine.begin() as connection:
        await connection.execute(text("TRUNCATE conversations, documents CASCADE"))


async def ask(
    service: ChatService,
    engine: AsyncEngine,
    question: ChatQuestion,
    conversation_id: uuid.UUID,
    mode: SearchMode,
) -> QuestionRun:
    request = AskRequest(
        conversation_id=conversation_id,
        question=unicodedata.normalize("NFC", question.question),
        client_message_id=uuid.uuid4(),
        retrieval=RetrievalOptions(mode=mode),
    )
    parts: list[str] = []
    sources = []
    cited: list[int] = []
    invalid = 0
    finished: Finished | None = None
    failed: Failed | None = None
    usage = None
    retrieval_ms = None
    async for event in service.answer(request):
        if isinstance(event, SourcesFound):
            sources = event.sources
            retrieval_ms = event.retrieval_ms
        elif isinstance(event, Delta):
            parts.append(event.text)
        elif isinstance(event, CitationsChecked):
            cited = [c.index for c in event.citations if c.supporting]
            invalid = event.invalid_markers_removed
        elif isinstance(event, UsageReported):
            usage = event.usage
        elif isinstance(event, Finished):
            finished = event
        elif isinstance(event, Failed):
            failed = event

    texts: dict[uuid.UUID, str] = {}
    if sources:
        sessions = create_session_factory(engine)
        async with sessions() as session:
            rows = await session.execute(
                select(Chunk.id, Chunk.text).where(Chunk.id.in_([s.chunk_id for s in sources]))
            )
            texts = {row.id: normalize_for_matching(row.text) for row in rows}
    records = [
        SourceRecord(
            index=s.index,
            filename=s.filename,
            chunk_ordinal=s.chunk_ordinal,
            retrieval_rank=s.retrieval_rank,
            evidence=[
                position
                for position, evidence in enumerate(question.evidence)
                if evidence.doc == s.filename
                and normalize_for_matching(evidence.anchor) in texts.get(s.chunk_id, "")
            ],
            numbers=sorted(set(numbers_in(texts.get(s.chunk_id, "")))),
        )
        for s in sources
    ]
    timings = finished.timings_ms if finished else {}
    run = QuestionRun(
        id=question.id,
        question=question.question,
        types=list(question.types),
        expected_language=question.answer_language,
        abstain_expected=question.abstain,
        status=finished.status if finished else "failed",
        grounding=finished.grounding if finished else None,
        error_code=failed.code if failed else None,
        answer="".join(parts),
        sources=records,
        cited=cited,
        invalid_markers_removed=invalid,
        served_model=finished.served_model if finished else None,
        input_tokens=usage.input_tokens if usage else None,
        output_tokens=usage.output_tokens if usage else None,
        first_token_ms=timings.get("first_token_ms"),
        total_ms=timings.get("total_ms"),
        retrieval_ms=retrieval_ms,
    )
    run.metrics = score(question, run)
    return run


def make_provider(kind: str, settings: Settings) -> LLMProvider:
    if kind == "extractive":
        return ExtractiveProvider()
    provider = create_llm_provider(settings)
    if provider is None:
        raise EvaluationError(
            "--provider configured needs the default model's provider to be configured "
            "(LLM_PROVIDER/LLM_MODEL, and ANTHROPIC_API_KEY, GEMINI_API_KEY or "
            "OLLAMA_BASE_URL/OLLAMA_MODELS)"
        )
    return provider


async def evaluate_chat(
    settings: Settings,
    provider_kind: str,
    mode: SearchMode,
    reset: bool,
    allow_fake: bool,
    only: Sequence[str] = (),
    dataset: ChatDataset | None = None,
) -> dict:  # type: ignore[type-arg]
    dataset = dataset or load_chat_dataset()
    fake = settings.embedding_provider == "fake"
    if fake and mode is not SearchMode.KEYWORD and not allow_fake:
        raise EvaluationError(
            "EMBEDDING_PROVIDER=fake gives meaningless semantic rankings; use --mode keyword, "
            "the real model, or --allow-fake-embeddings to test the harness"
        )
    questions = [q for q in dataset.questions if not only or q.id in only]
    missing_parents = {q.follow_up_of for q in questions if q.follow_up_of} - {
        q.id for q in questions
    }
    if missing_parents:
        raise EvaluationError(f"follow-up questions need {sorted(missing_parents)} too")
    llm = make_provider(provider_kind, settings)
    engine = create_engine(settings)
    embeddings = create_embedding_provider(settings)
    search = SearchService(settings, embeddings)
    runs: list[QuestionRun] = []
    started = time.perf_counter()
    try:
        if reset:
            await reset_chat_database(engine)
        with tempfile.TemporaryDirectory(prefix="yashrag-chat-eval-") as storage_dir:
            await ingest(load_dataset(), settings, engine, embeddings, Path(storage_dir))
        await search.warm_up()
        sessions = create_session_factory(engine)
        service = ChatService(settings, sessions, search, llm)
        conversations: dict[str, uuid.UUID] = {}
        for question in questions:
            if question.follow_up_of:
                conversation_id = conversations[question.follow_up_of]
            else:
                conversation_id = uuid.uuid4()
                async with sessions() as session:
                    session.add(Conversation(id=conversation_id, title=f"eval {question.id}"))
                    await session.commit()
            conversations[question.id] = conversation_id
            print(f"{question.id}...", file=sys.stderr, flush=True)
            runs.append(await ask(service, engine, question, conversation_id, mode))
        embedding_model = embeddings.load()
    finally:
        search.close()
        await llm.aclose()
        await engine.dispose()

    return {
        "metadata": {
            "run_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "git_commit": _git_commit(),
            "dataset_version": dataset.version,
            "dataset_sha256": dataset.sha256,
            "questions": len(runs),
            "provider": llm.name,
            "model": llm.model,
            "language_model": provider_kind != "extractive",
            "retrieval_mode": mode.value,
            "embedding": {
                "provider": settings.embedding_provider,
                "model": embedding_model.name,
                "revision": embedding_model.revision,
            },
            "fake_embeddings": fake,
            "settings": {
                "chunk_target_tokens": settings.chunk_target_tokens,
                "chat_retrieval_top_k": settings.chat_retrieval_top_k,
                "chat_min_similarity": settings.chat_min_similarity,
                "chat_context_token_budget": settings.chat_context_token_budget,
                "llm_max_output_tokens": settings.llm_max_output_tokens,
                "llm_effort": settings.llm_effort,
                "llm_temperature": settings.llm_temperature,
            },
            "wall_seconds": round(time.perf_counter() - started, 1),
            "host": {"platform": platform.platform(), "cpus": os.cpu_count()},
        },
        "summary": summarize(runs),
        "runs": [asdict(run) for run in runs],
    }


def _mean_of(runs: list[QuestionRun], key: str) -> float | None:
    values = [float(v) for r in runs if (v := r.metrics.get(key)) is not None]
    return round(mean(values), 3) if values else None


def _slice(runs: list[QuestionRun]) -> dict[str, float | int | None]:
    answerable = [r for r in runs if not r.abstain_expected]
    unanswerable = [r for r in runs if r.abstain_expected]
    return {
        "questions": len(runs),
        "answerable": len(answerable),
        "evidence_recall": _mean_of(answerable, "evidence_recall"),
        "fact_recall": _mean_of(answerable, "fact_recall"),
        "correct": _mean_of(answerable, "correct"),
        "citation_precision": _mean_of(answerable, "citation_precision"),
        "citation_coverage": _mean_of(answerable, "citation_coverage"),
        "cited_sentences": _mean_of(answerable, "cited_sentences"),
        "number_support": _mean_of(answerable, "number_support"),
        "false_abstention": _mean_of(answerable, "false_abstention"),
        "no_answer": len(unanswerable),
        "abstention_correct": _mean_of(unanswerable, "abstention_correct"),
        "language_ok": _mean_of(runs, "language_ok"),
        "completed": _mean_of(runs, "completed"),
    }


def summarize(runs: list[QuestionRun]) -> dict:  # type: ignore[type-arg]
    def timing(values: list[float | None]) -> dict[str, float | None]:
        present = [v for v in values if v is not None]
        if not present:
            return {"p50": None, "p95": None}
        return {"p50": round(percentile(present, 50), 1), "p95": round(percentile(present, 95), 1)}

    input_tokens = [r.input_tokens for r in runs if r.input_tokens]
    output_tokens = [r.output_tokens for r in runs if r.output_tokens]
    return {
        "all": _slice(runs),
        "by_type": {
            kind: _slice(selected)
            for kind in QUESTION_TYPES
            if (selected := [r for r in runs if kind in r.types])
        },
        "latency_ms": {
            "retrieval": timing([r.retrieval_ms for r in runs]),
            "first_token": timing([r.first_token_ms for r in runs]),
            "total": timing([r.total_ms for r in runs]),
        },
        "tokens": {
            "input_mean": round(mean(input_tokens)) if input_tokens else None,
            "output_mean": round(mean(output_tokens)) if output_tokens else None,
            "input_total": sum(input_tokens),
            "output_total": sum(output_tokens),
        },
        "invalid_markers_removed": sum(r.invalid_markers_removed for r in runs),
        "failed": [r.id for r in runs if r.status != "complete"],
    }


# ----------------------------------------------------------------------------- report


def _cell(value: float | int | None) -> str:
    if value is None:
        return "–"
    return f"{value:.3f}" if isinstance(value, float) else str(value)


def _ms(value: float | None) -> str:
    return "–" if value is None else f"{value:.1f}"


COLUMNS = (
    ("questions", "Qs"),
    ("evidence_recall", "Evidence recall"),
    ("fact_recall", "Fact recall"),
    ("correct", "Correct"),
    ("citation_precision", "Cit. precision"),
    ("citation_coverage", "Cit. coverage"),
    ("cited_sentences", "Cited sentences"),
    ("number_support", "Number support"),
    ("false_abstention", "False abstention"),
    ("abstention_correct", "Abstention (no_answer)"),
    ("language_ok", "Language"),
)


def render_report(result: dict) -> str:  # type: ignore[type-arg]
    meta = result["metadata"]
    summary = result["summary"]
    lines = [
        "# Answer evaluation",
        "",
        f"- Run: {meta['run_at']}, commit {meta['git_commit']}, "
        f"{meta['questions']} questions (dataset v{meta['dataset_version']}, "
        f"sha256 {meta['dataset_sha256'][:12]})",
        f"- Answers: {meta['provider']} / {meta['model']}",
        f"- Retrieval: {meta['retrieval_mode']}, embeddings {meta['embedding']['provider']} "
        f"({meta['embedding']['model']})",
    ]
    if not meta["language_model"]:
        lines += [
            "",
            "> **No language model was used.** These answers come from the extractive baseline,",
            "> which copies the source sentence that shares the most words with the question.",
            "> The numbers check the harness and show what retrieval plus copying achieves;",
            "> they are not a measure of any model's answer quality.",
        ]
    if meta["fake_embeddings"] and meta["retrieval_mode"] != "keyword":
        lines += ["", "> **Fake embeddings:** semantic retrieval numbers are meaningless."]
    lines += [
        "",
        "All scores are lexical and deterministic (no model-based judge). See",
        "docs/evaluation/chat.md for definitions and limits.",
        "",
        "| Slice | " + " | ".join(label for _, label in COLUMNS) + " |",
        "|---|" + "---:|" * len(COLUMNS),
    ]
    rows = [("all", summary["all"]), *summary["by_type"].items()]
    for name, values in rows:
        lines.append(f"| {name} | " + " | ".join(_cell(values[key]) for key, _ in COLUMNS) + " |")
    latency = summary["latency_ms"]
    tokens = summary["tokens"]
    lines += [
        "",
        "| Latency (ms) | p50 | p95 |",
        "|---|---:|---:|",
        *(
            f"| {name.replace('_', ' ')} | {_ms(v['p50'])} | {_ms(v['p95'])} |"
            for name, v in latency.items()
        ),
        "",
        f"Tokens: mean input {_cell(tokens['input_mean'])}, mean output "
        f"{_cell(tokens['output_mean'])}, total {tokens['input_total']} in / "
        f"{tokens['output_total']} out. Invalid citation markers removed: "
        f"{summary['invalid_markers_removed']}. Failed: {', '.join(summary['failed']) or 'none'}.",
        "",
        "## Answers",
        "",
    ]
    for run in result["runs"]:
        metrics = run["metrics"]
        verdict = (
            ("abstained ✓" if metrics.get("abstention_correct") else "answered ✗")
            if run["abstain_expected"]
            else ("correct ✓" if metrics.get("correct") else "incorrect ✗")
        )
        answer = " ".join(run["answer"].split())
        if len(answer) > 300:
            answer = answer[:299] + "…"
        lines += [
            f"**{run['id']}** ({', '.join(run['types'])}) {verdict}, grounding "
            f"{run['grounding']}, cited {run['cited'] or 'nothing'}",
            "",
            f"> Q: {run['question']}",
            ">",
            f"> A: {answer or '(empty)'}",
            "",
        ]
    return "\n".join(lines)


# -------------------------------------------------------------------------------- CLI


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--provider",
        choices=("extractive", "configured"),
        default="extractive",
        help="extractive: the non-LLM baseline; configured: the default model in the settings "
        "(LLM_PROVIDER and LLM_MODEL choose Claude, Gemini or an Ollama model)",
    )
    parser.add_argument("--mode", choices=[m.value for m in SearchMode], default=None)
    parser.add_argument("--questions", default="", help="comma-separated ids (default: all)")
    parser.add_argument("--output-dir", type=Path, default=Path("evaluation/results/chat"))
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--allow-fake-embeddings", action="store_true")
    args = parser.parse_args(argv)
    settings = get_settings()
    mode = SearchMode(args.mode or settings.chat_retrieval_mode or settings.search_default_mode)
    only = [q.strip() for q in args.questions.split(",") if q.strip()]
    try:
        result = asyncio.run(
            evaluate_chat(
                settings, args.provider, mode, args.reset, args.allow_fake_embeddings, only
            )
        )
    except EvaluationError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    args.output_dir.mkdir(parents=True, exist_ok=True)
    (args.output_dir / "results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    report = render_report(result)
    (args.output_dir / "report.md").write_text(report, encoding="utf-8")
    print(report)
    print(f"wrote {args.output_dir}/results.json and report.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
