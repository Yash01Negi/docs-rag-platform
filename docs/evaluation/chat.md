# Answer evaluation (Phase 4)

How grounded answers are measured, and what has been measured so far. Retrieval-only
evaluation is in [README.md](README.md).

> **Real-model answer quality has not been measured yet.** No Anthropic API key was
> available in the development environment or as a GitHub Actions secret, so every number
> below comes from a non-LLM extractive baseline. Those numbers test the harness and the
> retrieval half of the pipeline; they say nothing about how well a language model
> answers. Instructions for the real-model run are below.

## The evaluation set

[`backend/evaluation/dataset/chat_qa.toml`](../../backend/evaluation/dataset/chat_qa.toml)
holds 26 questions written for this project about the 13-document evaluation corpus
(English, Japanese and mixed-language company documents). Each question is judged by hand:

- **evidence**: the passages that answer it, as exact quotes (anchors) from a document;
- **facts**: what a correct answer must state, each with its accepted spellings
  (for example `["3 days", "three days"]`, `["3000円", "3千円", "三千円"]`);
- **abstain**: for questions the corpus cannot answer;
- **follow_up_of**: asked in the same conversation, right after another question.

| Type | Questions | What it tests |
| --- | ---: | --- |
| `en_en` | 7 | English question, English evidence |
| `ja_ja` | 6 | Japanese question, Japanese evidence |
| `en_to_ja` | 5 | English question, Japanese evidence (answer in English) |
| `ja_to_en` | 4 | Japanese question, English evidence (answer in Japanese) |
| `mixed` | 4 | The question mixes Japanese and English |
| `multi_source` | 2 | Evidence in two documents, one in each language |
| `follow_up` | 1 | "And for international ones?" after a per-diem question |
| `distractor` | 7 | Another document shares the question's words but does not answer it |
| `no_answer` | 4 | Nothing in the corpus answers it |

A question can have several types. The loader checks every anchor against its document,
that follow-ups come right after their first question, that abstention matches the type,
and that every type is covered.

## Metrics

All scores are **lexical and deterministic**. There is no model-based judge, so no score is
a model's opinion of an answer, and a run can be repeated exactly.

| Metric | Definition (answerable questions unless noted) |
| --- | --- |
| Evidence recall | Judged evidence passages present in the sources given to the model (a source counts when its chunk contains the anchor) |
| Fact recall | Required facts found in the answer, after NFKC, case folding and removing spaces and digit-group commas |
| Correct | The answer completed, did not abstain, and contains every required fact |
| Citation precision | Cited sources that contain judged evidence. Answers that cite nothing are left out |
| Citation coverage | Judged evidence passages that the answer cites |
| Cited sentences | Answer sentences carrying at least one `[S<n>]` marker |
| Number support | A faithfulness proxy: numbers in the answer that also appear in a cited source |
| False abstention | Answerable questions answered with "not in the documents" (grounding `insufficient_evidence` or `no_sources`) |
| Abstention (no_answer) | `no_answer` questions answered with "not in the documents" |
| Language | The answer is mainly in the question's language (Japanese share of letters ≥ 0.5 means Japanese) |
| Latency | Retrieval, time to first token and total time, p50 and p95 (nearest rank) |
| Tokens | Input and output tokens as reported by the provider |

**Known limits of these measures.** Fact recall can be fooled: an answer can contain the
right words and still be wrong ("not 3 days"), or state a fact in a spelling the judgments
did not list (then it is counted as missing). Citation precision proves that a cited
passage was judged evidence, not that it supports the sentence it is attached to. Number
support only checks digits (a "three days" claim against "三日分" is not checked). A
model-based or human review of the answers is the next step for faithfulness and
correctness; the harness writes every answer into `report.md` for that.

## How to run

```bash
cd backend
# A migrated database whose name ends in _eval or _test (--reset deletes its documents
# and conversations):
export DATABASE_URL=postgresql+psycopg://…/yashrag_eval
uv run alembic upgrade head

# Deterministic, no API key, no model download (keyword retrieval, extractive baseline):
ENVIRONMENT=test EMBEDDING_PROVIDER=fake \
  uv run python -m evaluation.chat_eval --reset --provider extractive --mode keyword

# Real retrieval (BGE-M3), extractive baseline:
uv run --extra embeddings python -m evaluation.chat_eval --reset --provider extractive --mode semantic

# Real retrieval and the configured model (costs money: 26 answers):
ANTHROPIC_API_KEY=… uv run --extra embeddings python -m evaluation.chat_eval --reset \
  --provider configured --mode semantic
```

Each run writes `results.json` (every answer, source, citation and score) and `report.md`
to `--output-dir` (default `evaluation/results/chat`, ignored by Git). `--questions
en-en-01,ja-ja-01` runs a subset. The report says in bold when no language model was used.

The CI suite runs the deterministic mode against the test database
(`tests/test_chat_eval.py`), with regression floors for evidence recall and citation
precision, so pipeline changes that break retrieval or citations fail CI. The real-model
mode never runs in CI.

## Results

### Extractive baseline, real BGE-M3 retrieval (GitHub Actions)

Run on GitHub Actions (4 vCPUs, AMD EPYC 9V45, CPU only) on 2026-10-12 at commit `042488d`,
semantic retrieval with BAAI/bge-m3 (revision `5617a9f`), production chunking (400
tokens), top 8, dataset sha256 `4cf25f4cf4fb…`. **No language model**: the answers come
from `evaluation/extractive.py`, which copies the source sentence sharing the most words
with the question.

| Slice | Qs | Evidence recall | Fact recall | Correct | Cit. precision | Cit. coverage | False abstention | Abstention (no_answer) | Language |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| all | 26 | 1.000 | 0.364 | 0.364 | 0.900 | 0.409 | 0.545 | 0.750 | 0.885 |
| en_en | 7 | 1.000 | 0.714 | 0.714 | 1.000 | 0.714 | 0.286 | – | 1.000 |
| ja_ja | 6 | 1.000 | 0.333 | 0.333 | 1.000 | 0.333 | 0.667 | – | 1.000 |
| en_to_ja | 5 | 1.000 | 0.000 | 0.000 | 0.500 | 0.200 | 0.600 | – | 0.800 |
| ja_to_en | 4 | 1.000 | 0.000 | 0.000 | – | 0.000 | 1.000 | – | 1.000 |
| mixed | 4 | 1.000 | 0.333 | 0.333 | 1.000 | 0.667 | 0.333 | 1.000 | 0.250 |
| multi_source | 2 | 1.000 | 0.000 | 0.000 | – | 0.000 | 1.000 | – | 1.000 |
| follow_up | 1 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 | 0.000 | – | 1.000 |
| distractor | 7 | 1.000 | 0.250 | 0.250 | 1.000 | 0.250 | 0.750 | 0.667 | 0.857 |
| no_answer | 4 | – | – | – | – | – | – | 0.750 | 0.750 |

Retrieval p50 138.8 ms, p95 165.7 ms; whole pipeline (retrieval, prompt, the baseline's
answer, two transactions) p50 145.3 ms, p95 172.1 ms. Cited sentences and number support
were 1.000 wherever defined. No invalid markers, no failures.

What this shows:

- **Retrieval puts the evidence in front of the model for every question**, in every
  language direction, including both two-document questions and the follow-up (which is
  only retrievable because the previous question is added to the search). With keyword
  retrieval instead, evidence recall drops to 0.727 overall and to 0.300 and 0.375 for the
  two cross-language slices (sandbox run, same day), which is why semantic retrieval is the
  default for chat.
- The baseline itself is weak in exactly the places a language model is needed: it cannot
  translate (0 correct across languages), combine two passages, or recognise that a
  sentence mentioning the same words does not answer the question (`none-03`). Its numbers
  are a floor and a harness check, not a target.

### Configured model

**Not run.** The `ANTHROPIC_API_KEY` secret is not set in the repository, and no key was
available in the development environment. The temporary workflow that ran the baseline
also checked for the secret and skipped the model step.

To produce these numbers: run the third command above with a key, or add the
`ANTHROPIC_API_KEY` repository secret and run the same evaluation in a GitHub Actions job.
Record the commit, model, served model, settings, and the full table here, and keep the
baseline for comparison.

## Real-model results

None yet; see "Configured model" above. Until a run is recorded here, the following are
**unverified**: answer correctness and faithfulness with a real model, citation behaviour
of a real model (whether it cites, and how often invalid markers are removed), abstention
rates, answer-language compliance, real time-to-first-token and total latency, token use
and cost, and resistance to prompt injection beyond the deterministic defences described in
[../chat.md](../chat.md#grounding-rules-and-prompt-injection).
