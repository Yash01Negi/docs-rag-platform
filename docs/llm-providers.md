# Language model providers

The chat pipeline talks to one interface, `LLMProvider`
([`backend/app/llm/base.py`](../backend/app/llm/base.py)). Anthropic Claude is the only
implementation ([`backend/app/llm/anthropic.py`](../backend/app/llm/anthropic.py)), built
on the official `anthropic` Python SDK (`anthropic>=1.13,<2`, locked in `uv.lock`).

## The interface

```python
class LLMProvider(ABC):
    name: str    # "anthropic"
    model: str   # the configured model ID

    def stream(self, request: GenerationRequest) -> AsyncIterator[TextDelta | GenerationDone]: ...
    async def generate(self, request: GenerationRequest) -> GenerationResult: ...  # built on stream()
    async def aclose(self) -> None: ...
```

- `GenerationRequest` holds the system prompt, the messages (`user` / `assistant` turns of
  plain text) and `max_output_tokens`. Nothing in it is provider-specific.
- `stream()` yields `TextDelta`s and then exactly one `GenerationDone` with a
  provider-neutral `stop_reason` (`end`, `max_tokens`, `refusal`, `other`), the provider's
  raw stop reason, token `Usage`, the model that actually answered, the provider's request
  ID, and whether a fallback model was used.
- Failures raise `ProviderError` with a `ProviderErrorCode` (`provider_rate_limited`,
  `provider_overloaded`, `provider_timeout`, `provider_connection_error`,
  `provider_authentication_failed`, `provider_permission_denied`, `provider_bad_request`,
  `provider_model_not_found`, `provider_request_too_large`, `provider_server_error`,
  `provider_invalid_response`), a message that is safe to show users, `retryable`, and
  `retry_after_seconds` when the provider says. The provider's own error text is never put
  in the message.
- Closing the iterator early (the user pressed Stop or disconnected) must cancel the
  underlying request.
- One provider instance (and one HTTP client) is created at startup and shared; a client
  is never created per request or per token. `aclose()` releases it at shutdown.

## Anthropic configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `LLM_PROVIDER` | `anthropic` | The only implemented provider |
| `ANTHROPIC_API_KEY` | unset | Server-side only. Without it the API starts, `/chat/options` reports `configured: false`, and asking answers `503 llm_not_configured` |
| `LLM_MODEL` | `claude-opus-5-5` | Model ID sent to the API |
| `LLM_MAX_OUTPUT_TOKENS` | `2048` | Longest answer; longer answers stop with `stop_reason: max_tokens` |
| `LLM_TEMPERATURE` | unset | Only for models that accept it (current Claude models reject sampling parameters); sent as an extra body field |
| `LLM_EFFORT` | unset | `low` … `max`, sent as `output_config.effort`; unset uses the model's default |
| `LLM_FALLBACKS` | `default` | Server-side fallback when the requested model declines a request on policy grounds (`fallbacks: "default"` with the `server-side-fallback-2026-07-01` beta); `none` turns it off. The served model is recorded and shown |
| `LLM_CONNECT_TIMEOUT_SECONDS` | `10` | Connection timeout |
| `LLM_READ_TIMEOUT_SECONDS` | `60` | Longest wait for the next piece of a streamed response |
| `LLM_REQUEST_TIMEOUT_SECONDS` | `180` | Deadline for one whole answer, retries included |
| `LLM_MAX_RETRIES` | `2` | SDK retries (with backoff, honouring `retry-after`) for connection errors, 408, 409, 429 and 5xx before any text has streamed |

The SDK also honours its standard `ANTHROPIC_BASE_URL` environment variable (for example
for a gateway). Thinking is left at the model's default; thinking blocks, if any, are
dropped by the adapter and never shown, logged or stored.

Errors are classified by the SDK's exception class and, for errors that arrive inside an
HTTP 200 stream, by their `type` (`rate_limit_error`, `overloaded_error`, …). After the
first token a failure is reported, not retried, because part of the answer has already
been shown and stored.

## Adding another provider

OpenAI, Amazon Bedrock or a local model (for example through an OpenAI-compatible local
server) would each be one new class implementing `stream()` and mapping its errors to
`ProviderErrorCode`, plus a branch in `create_llm_provider`
([`backend/app/llm/__init__.py`](../backend/app/llm/__init__.py)) and its settings. The
prompt uses no provider-specific features (plain system and user/assistant text, markers
in the text), so the chat pipeline, persistence, SSE protocol and UI do not change. Prompt
wording and the citation rate would need to be re-measured with the evaluation harness for
each model.

There is deliberately **no stand-in provider** in the application: without credentials,
chat is unavailable rather than answered by something that is not a model. The test-only
`ScriptedProvider` (`backend/tests/chat_fakes.py`) and the evaluation-only
`ExtractiveProvider` (`backend/evaluation/extractive.py`) are not importable from the app
configuration.

## Verification status

- **Verified with test doubles:** the provider contract (`tests/test_llm_anthropic.py`)
  drives the real `anthropic` SDK against a mocked HTTP transport (`httpx2.MockTransport`)
  that returns recorded-format Server-Sent Events: request parameters, streaming text,
  usage, stop reasons, fallback detection, the request ID, every error class (status
  errors, mid-stream errors on a 200 response, timeouts, connection errors), SDK retries
  of a rate limit before streaming, that closing the stream early stops reading, and that
  the API key never appears in logs. The chat pipeline and API tests use
  `ScriptedProvider`.
- **Not verified in this environment:** a call to the real Anthropic API. The development
  environment has no API key, and real-provider inference was not run here. See
  [evaluation/chat.md](evaluation/chat.md#real-model-results) for whether a real-model
  evaluation has been run since.
