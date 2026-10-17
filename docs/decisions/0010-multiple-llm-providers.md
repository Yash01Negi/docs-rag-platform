# ADR 0010: Several LLM providers, chosen per question from a server-side catalog

- **Status:** Accepted
- **Date:** 2026-10-13
- **Extends:** [ADR 0007](0007-llm-provider.md)

## Context

Phase 4 answered with Anthropic Claude only, which needs a paid API key. Phase 4.1 adds
Google Gemini (with a free tier) and local models through Ollama, so the platform can be
used without paying per question and with documents that must not leave the machine. Users
should be able to pick a model for each question, including within one conversation. The
browser must not be able to make the server call an arbitrary provider, model or URL, keys
must stay on the server, and a user who picked a free or local model must never be billed
on a paid one behind their back.

## Decision

- **One interface, three adapters.** `LLMProvider` (ADR 0007) is unchanged. Gemini uses the
  official Google Gen AI SDK (`google-genai`) against the Gemini Developer API; Ollama uses
  its HTTP API (`/api/chat`, NDJSON) through `httpx2`, which is already a dependency. No
  OpenAI-compatible shim is used for either, so each provider's own error, usage and
  safety signals are mapped exactly.
- **A registry built from configuration.** `LLMRegistry` holds one backend per provider,
  each with an allowlist of model IDs from settings (`ANTHROPIC_MODELS`, `GEMINI_MODELS`,
  `OLLAMA_MODELS`), one shared client, a cost class (`paid`, `free_tier`, `local`) and an
  availability check against the provider (Models API, `models.get`, `/api/tags`). Model
  IDs are settings with documented defaults, not code, because providers retire models.
- **The browser picks, the server decides.** `GET /chat/models` publishes the catalog
  (without keys or URLs). A question may carry `{"provider", "model"}`; the server accepts
  only a listed, configured, not-unavailable pair (`422 invalid_model`,
  `503 provider_not_configured`, `503 model_unavailable`). Without a choice it uses
  `LLM_PROVIDER` / `LLM_MODEL`, and fails with Phase 4's `503 llm_not_configured` when that
  provider has no credentials.
- **No cross-provider fallback.** A failure is reported to the user with a Retry button,
  never retried on another provider or model. Anthropic's own server-side fallback for
  policy refusals stays (Claude to Claude, recorded and shown).
- **Record what was asked and what answered.** Each answer stores `requested_provider` and
  `requested_model` (migration 0005) beside `provider`, `model` and `served_model`.
- **Ollama defaults for RAG.** `num_ctx` is set (16384) because Ollama's default context
  silently truncates long prompts; Ollama cloud models are refused unless explicitly
  allowed, so "local" means local.

## Consequences

- With only `ANTHROPIC_API_KEY` set, behaviour, endpoints and errors are as in Phase 4; the
  selector simply lists Claude models (and shows the other providers as not configured).
- Answer quality, citation rate and abstention differ between models and have to be
  measured per model with the evaluation harness; small local models follow the citation
  format less reliably. None of this has been measured yet.
- Gemini's free tier has low rate limits and lets Google use submitted content; the UI and
  docs warn about it, but the operator decides whether to enable it
  ([llm-providers.md](../llm-providers.md#free-tier-limits-and-data-handling)).
- Availability checks add a few provider calls every `LLM_CATALOG_TTL_SECONDS`; a failed
  check marks models `unknown` rather than blocking them.
- Real-provider behaviour is verified only with mocked transports until keys and an Ollama
  server are available ([llm-smoke-tests.md](../llm-smoke-tests.md)).
