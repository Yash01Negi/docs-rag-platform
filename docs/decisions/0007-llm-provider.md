# ADR 0007: A provider-neutral LLM interface, with Anthropic Claude through the official SDK

- **Status:** Accepted
- **Date:** 2026-10-12

## Context

Phase 4 generates answers with a language model. Anthropic Claude is the primary provider,
but OpenAI, Amazon Bedrock or a local model must be possible later without rewriting the
chat pipeline. API keys must stay on the server, and the application must behave well when
the provider is slow, rate-limited, failing, or not configured at all.

## Decision

- The chat pipeline depends only on `LLMProvider` (`backend/app/llm/base.py`): a streaming
  `stream()` that yields text deltas and one final event (stop reason, usage, served model,
  provider request ID), a non-streaming `generate()` built on it, and provider-neutral
  `ProviderError` codes with user-safe messages. Prompts are plain system and
  user/assistant text, with citation markers in the text, so they carry no provider
  features.
- `AnthropicProvider` uses the official `anthropic` SDK (locked in `uv.lock`), one
  `AsyncAnthropic` client shared by all requests, the Messages streaming API, and the SDK's
  own retries for connection errors, 408, 409, 429 and 5xx before the first token. Errors
  after the first token are reported, not retried.
- Default model `claude-opus-5-5`; model, output limit, effort, temperature (only for
  models that accept it), timeouts, retries and server-side fallback are settings. The
  model that actually answered is stored with each message, and fallback is recorded.
- Without `ANTHROPIC_API_KEY` the API starts, reports `configured: false`, and refuses to
  answer (`503 llm_not_configured`). There is no stand-in "answer" provider in the
  application: test doubles live only in `tests/` and `evaluation/`.

## Consequences

- A second provider is one class and one factory branch; the pipeline, persistence, SSE
  protocol and UI stay as they are. Its prompt behaviour (citation rate, abstention) has to
  be measured with the evaluation harness before it is trusted.
- Real-provider behaviour is verified only through contract tests that drive the real SDK
  against a mocked HTTP transport, until a key is available (see
  [llm-providers.md](../llm-providers.md#verification-status)).
- Rate limits and outages surface to users as retryable errors with a Retry button; there
  is no queue or second provider to fail over to.
