# Optional real-provider smoke tests

CI never calls a real language model. Every provider is tested there against a mocked HTTP
transport ([llm-providers.md](llm-providers.md#verification-status)). The smoke tests in
[`backend/tests/test_llm_live_smoke.py`](../backend/tests/test_llm_live_smoke.py) make
**real requests** to whichever providers you configure, to confirm that a key, a model ID
or an Ollama install actually works.

> **Status:** these tests have **not** been run against any real provider by the project's
> automated work so far. No API keys or Ollama server were available. Results below are
> for you to fill in when you run them.

## What they check

For each configured provider, using the first model in its list:

1. The availability check reports the model as `available` (the key can see it, or Ollama
   has it installed).
2. A short streamed request ("Reply with the single word: pong") ends normally, contains
   "pong", and reports input and output token usage.
3. A longer stream can be stopped after the first piece of text without hanging.

Each provider sends two small requests per run (plus one availability check). Claude and
paid Gemini bill them; free-tier Gemini counts them against the per-minute and per-day
limits; Ollama is free but loads the model, which can take a minute the first time.

## Running them

From `backend/`. The tests are skipped unless `RUN_LLM_SMOKE_TESTS=1`, and each provider is
skipped unless it is configured in the environment (or the root `.env`):

```bash
# Gemini (free tier is fine)
RUN_LLM_SMOKE_TESTS=1 GEMINI_API_KEY=… uv run pytest -m live_llm -s -k gemini

# Ollama on this Mac (start the app and `ollama pull qwen3:8b` first)
RUN_LLM_SMOKE_TESTS=1 OLLAMA_BASE_URL=http://localhost:11434 OLLAMA_MODELS=qwen3:8b \
  uv run pytest -m live_llm -s -k ollama

# Claude (paid)
RUN_LLM_SMOKE_TESTS=1 ANTHROPIC_API_KEY=… uv run pytest -m live_llm -s -k anthropic

# A different model than the first in the default list
RUN_LLM_SMOKE_TESTS=1 GEMINI_API_KEY=… GEMINI_MODELS=gemini-3.5-flash-lite \
  uv run pytest -m live_llm -s -k gemini
```

`-s` prints the served model, token usage and the text for each provider. Pass keys on the
command line or from `.env`; never commit them.

## End-to-end check in the app

The smoke tests exercise the adapters only. To check the whole path:

1. Configure the provider in `.env` and start the stack (README, Getting started).
2. Upload a document, open **Chat**, pick the model in the selector, and ask a question
   about the document.
3. Confirm the answer streams, cites the document (`[1]` buttons open the passage), and
   shows "Answered by …" with the model you picked. Switch to another model and ask a
   follow-up in the same conversation.
4. Press **Stop** during an answer; it should be stored as stopped.

## In GitHub Actions (optional)

To run them without keys on your machine, a maintainer can add a temporary workflow that
sets `RUN_LLM_SMOKE_TESTS=1` and reads `GEMINI_API_KEY` / `ANTHROPIC_API_KEY` from
repository secrets, run it once with `workflow_dispatch`, and delete it afterwards. Ollama
cannot be tested that way without installing it on the runner.

## Recording results

When you run them, note the date, provider, model, and pass or fail here or in the PR, so
the verification status in [llm-providers.md](llm-providers.md#verification-status) can
be updated with what was actually tested.
