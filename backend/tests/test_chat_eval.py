"""The answer evaluation set, its lexical scores, the extractive baseline and a harness run.

None of these tests calls a language model: the harness runs with the extractive baseline,
which copies source sentences. Real-model runs are documented in docs/evaluation/chat.md.
"""

import asyncio
import shutil
from pathlib import Path

import pytest

from app.chat.prompt import PromptSource, build_messages
from app.core.config import Settings
from app.db.models import Language
from app.llm.base import GenerationRequest, TextDelta
from app.search.service import SearchMode
from evaluation.chat_dataset import QUESTION_TYPES, ChatQuestion, Evidence, load_chat_dataset
from evaluation.chat_eval import (
    QuestionRun,
    SourceRecord,
    answer_language,
    answer_sentences,
    evaluate_chat,
    fact_present,
    numbers_in,
    render_report,
    score,
)
from evaluation.dataset import DATASET_DIR, DatasetError
from evaluation.extractive import ExtractiveProvider, extract, terms
from evaluation.run import EvaluationError

# ------------------------------------------------------------------------------ dataset


def test_chat_dataset_is_valid_and_covers_every_type() -> None:
    dataset = load_chat_dataset()
    assert {t for q in dataset.questions for t in q.types} == set(QUESTION_TYPES)
    assert {q.answer_language for q in dataset.questions} == {"en", "ja"}
    assert sum(q.abstain for q in dataset.questions) >= 3


def _copy(tmp_path: Path) -> Path:
    target = tmp_path / "dataset"
    shutil.copytree(DATASET_DIR, target)
    return target


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('retained for 35 days" }]', 'retained for 36 days" }]', "not in"),
        ('id = "en-en-02"', 'id = "en-en-01"', "duplicate"),
        ('follow_up_of = "en-en-04"', 'follow_up_of = "en-en-02"', "right after"),
        ('types = ["no_answer"]\nabstain = true', 'types = ["en_en"]\nabstain = true', "abstain"),
        ('answer_language = "en"', 'answer_language = "fr"', "answer language"),
        ('facts = [["60"]]', "facts = [[]]", "spelling"),
    ],
)
def test_chat_dataset_check_rejects_broken_judgments(
    tmp_path: Path, old: str, new: str, message: str
) -> None:
    directory = _copy(tmp_path)
    path = directory / "chat_qa.toml"
    source = path.read_text(encoding="utf-8")
    assert old in source
    path.write_text(source.replace(old, new, 1), encoding="utf-8")
    with pytest.raises(DatasetError, match=message):
        load_chat_dataset(directory)


# ------------------------------------------------------------------------------- scores


def test_facts_match_across_width_spacing_and_digit_grouping() -> None:
    assert fact_present("通信費手当は月額3,000 円です。", ["3000円"])
    assert fact_present("週３日まで", ["3日"])
    assert fact_present("Up to Three Days a week", ["three days"])
    assert not fact_present("週2日まで", ["3日", "三日"])


def test_sentences_keep_their_markers_and_numbers_ignore_them() -> None:
    assert answer_sentences("Snapshots last 35 days. [S1] Monthly ones 13 months.[S2]") == [
        "Snapshots last 35 days.[S1]",
        "Monthly ones 13 months.[S2]",
    ]
    assert answer_sentences("週3日までです。[S1]\n- 例外あり") == [
        "週3日までです。[S1]",
        "- 例外あり",
    ]
    assert numbers_in("月額3,000円 [S12]") == ["3000"]
    assert answer_language("在宅勤務は週3日までです [S1]") == "ja"
    assert answer_language("Up to 3 days (週3日) [S1]") == "en"
    assert answer_language("[S1]") is None


def _question(**overrides: object) -> ChatQuestion:
    values: dict[str, object] = {
        "id": "q",
        "question": "How long?",
        "answer_language": "en",
        "types": ("en_en",),
        "evidence": (Evidence("a.md", "35 days"), Evidence("b.md", "13 months")),
        "facts": (("35 days",), ("13 months",)),
    }
    values.update(overrides)
    return ChatQuestion(**values)  # type: ignore[arg-type]


def _run(answer: str, cited: list[int], grounding: str = "grounded") -> QuestionRun:
    sources = [
        SourceRecord(1, "a.md", 0, 1, evidence=[0], numbers=["35"]),
        SourceRecord(2, "c.md", 0, 2, evidence=[], numbers=["7"]),
        SourceRecord(3, "b.md", 0, 3, evidence=[1], numbers=["13"]),
    ]
    return QuestionRun(
        id="q",
        question="How long?",
        types=["en_en"],
        expected_language="en",
        abstain_expected=False,
        status="complete",
        grounding=grounding,
        error_code=None,
        answer=answer,
        sources=sources,
        cited=cited,
        invalid_markers_removed=0,
        served_model="m",
        input_tokens=1,
        output_tokens=1,
        first_token_ms=1.0,
        total_ms=2.0,
        retrieval_ms=1.0,
    )


def test_score_measures_citations_against_judged_evidence() -> None:
    metrics = score(_question(), _run("Kept 35 days [S1]. Staging keeps 7 days [S2].", [1, 2]))
    assert metrics["evidence_recall"] == 1.0
    assert metrics["fact_recall"] == 0.5
    assert metrics["correct"] is False
    assert metrics["citation_precision"] == 0.5  # S2 holds no judged evidence
    assert metrics["citation_coverage"] == 0.5  # the 13-month passage is not cited
    assert metrics["cited_sentences"] == 1.0
    assert metrics["number_support"] == 1.0

    metrics = score(_question(), _run("35 days [S1], 13 months [S3], or 99 years.", [1, 3]))
    assert metrics["correct"] is True
    assert metrics["citation_precision"] == 1.0
    assert metrics["number_support"] == pytest.approx(2 / 3)  # 99 is in no cited source


def test_score_counts_abstentions() -> None:
    abstained = _run("The documents do not say.", [], grounding="insufficient_evidence")
    assert score(_question(), abstained)["false_abstention"] is True
    no_answer = _question(types=("no_answer",), abstain=True, evidence=(), facts=())
    assert score(no_answer, abstained)["abstention_correct"] is True
    assert score(no_answer, _run("Probably 3 weeks.", []))["abstention_correct"] is False


# ---------------------------------------------------------------------------- baseline


def _request(question: str, texts: list[str], language: Language) -> GenerationRequest:
    sources = [
        PromptSource(i, f"doc{i}.md", text, "en", None, None, None)
        for i, text in enumerate(texts, 1)
    ]
    return GenerationRequest("system", build_messages([], question, sources, language), 512)


def test_extractive_baseline_copies_the_best_sentence_or_abstains() -> None:
    assert {"在宅", "勤務", "日"} <= terms("在宅勤務は週3日")
    picks = extract(
        "How long are snapshots retained?",
        [(1, "Snapshots are retained for 35 days. Staging differs."), (2, "Unrelated.")],
    )
    assert [(p.index, p.sentence) for p in picks] == [(1, "Snapshots are retained for 35 days.")]

    async def answer(request: GenerationRequest) -> str:
        return "".join(
            [e.text async for e in ExtractiveProvider().stream(request) if isinstance(e, TextDelta)]
        )

    found = asyncio.run(
        answer(
            _request(
                "在宅勤務は週に何日？", ["在宅勤務は原則として週3日までとする。"], Language.JAPANESE
            )
        )
    )
    assert found == "在宅勤務は原則として週3日までとする。 [S1]"
    missing = asyncio.run(
        answer(_request("parental leave weeks?", ["Office hours."], Language.ENGLISH))
    )
    assert missing.startswith("[NOT_FOUND] ")


# ------------------------------------------------------------------------------ harness


@pytest.mark.integration
def test_answer_evaluation_runs_end_to_end_with_the_baseline(db_settings: Settings) -> None:
    result = asyncio.run(
        evaluate_chat(db_settings, "extractive", SearchMode.KEYWORD, reset=True, allow_fake=False)
    )
    dataset = load_chat_dataset()
    meta = result["metadata"]
    assert meta["language_model"] is False and meta["provider"] == "extractive-baseline"
    assert meta["questions"] == len(dataset.questions)
    summary = result["summary"]
    assert summary["failed"] == []
    assert set(summary["by_type"]) == set(QUESTION_TYPES)
    runs = {run["id"]: run for run in result["runs"]}
    # Every citation maps to a source that was given to the model.
    for run in runs.values():
        assert set(run["cited"]) <= {source["index"] for source in run["sources"]}
    # The follow-up is retrieved with the previous question as context and answered.
    assert runs["en-en-05"]["metrics"]["correct"] is True
    # Regression floors for the deterministic pipeline, not quality targets.
    assert summary["by_type"]["en_en"]["evidence_recall"] >= 0.8
    assert summary["by_type"]["ja_ja"]["evidence_recall"] >= 0.8
    assert summary["all"]["citation_precision"] >= 0.8

    report = render_report(result)
    assert "No language model was used" in report
    assert "| all | 26 |" in report


@pytest.mark.integration
def test_harness_needs_a_configured_model_for_a_real_run(db_settings: Settings) -> None:
    with pytest.raises(EvaluationError, match="ANTHROPIC_API_KEY"):
        asyncio.run(
            evaluate_chat(
                db_settings, "configured", SearchMode.KEYWORD, reset=True, allow_fake=False
            )
        )
    with pytest.raises(EvaluationError, match="meaningless"):
        asyncio.run(
            evaluate_chat(
                db_settings, "extractive", SearchMode.HYBRID, reset=True, allow_fake=False
            )
        )
