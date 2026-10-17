# Language model providers

The chat pipeline talks to one interface, `LLMProvider`
([`backend/app/llm/base.py`](../backend/app/llm/base.py)). Three providers implement it:

| Provider | Module | Client | Cost class |
| --- | --- | --- | --- |
| Anthropic Claude | [`llm/anthropic.py`](../backend/app/llm/anthropic.py) | official `anthropic` SDK | paid |
| Google Gemini | [`llm/gemini.py`](../backend/app/llm/gemini.py) | official Google Gen AI SDK (`google-genai`), Gemini Developer API | free tier or paid |
| Ollama | [`llm/ollama.py`](../backend/app/llm/ollama.py) | Ollama's HTTP API (`POST /api/chat`) over `httpx2` | local |

All SDK versions are locked in `backend/uv.lock`. The browser chooses a provider and model
for each question from a catalog the server builds from its own configuration
([ADR 0010](decisions/0010-multiple-llm-providers.md)); a question that names none uses
`LLM_PROVIDER` / `LLM_MODEL`. With only `ANTHROPIC_API_KEY` set, the application behaves
exactly as in Phase 4.

## The interface

```python
class LLMProvider(ABC):
    name: str    # "anthropic", "gemini" or "ollama"
    model: str   # the model ID this instance sends

    def stream(self, request: GenerationRequest) -> AsyncIterator[TextDelta | GenerationDone]: ...
    async def generate(self, request: GenerationRequest) -> GenerationResult: ...  # built on stream()
    async def aclose(self) -> None: ...
```

- `GenerationRequest` holds the system prompt, the messages (`user` / `assistant` turns of
  plain text) and `max_output_tokens`. Nothing in it is provider-specific.
- `stream()` yields `TextDelta`s and then exactly one `GenerationDone` with a
  provider-neutral `stop_reason` (`end`, `max_tokens`, `refusal`, `other`), the provider's
  raw stop reason, token `Usage`, the model that actually answered, the provider's request
  ID when it has one, and whether a fallback model was used.
- Failures raise `ProviderError` with a `ProviderErrorCode` (`provider_rate_limited`,
  `provider_overloaded`, `provider_timeout`, `provider_connection_error`,
  `provider_authentication_failed`, `provider_permission_denied`, `provider_bad_request`,
  `provider_model_not_found`, `provider_request_too_large`, `provider_server_error`,
  `provider_invalid_response`), a message that is safe to show users, `retryable`, and
  `retry_after_seconds` when the provider says. The provider's own error text is never put
  in the message.
- Closing the iterator early (the user pressed Stop or disconnected) must cancel the
  underlying request.
- Thinking or reasoning output, from any provider, is dropped by the adapter and never
  shown, logged or stored.

## Registry and catalog

[`llm/registry.py`](../backend/app/llm/registry.py) holds one `ProviderBackend` per
provider. Each backend:

- knows its allowlist of model IDs (from settings), whether it is configured, its cost
  class, and a short notice about cost and data use for the selector;
- creates one HTTP client at first use, shared by all of its models and requests, and
  closes it at shutdown;
- can `check()` which of its models are available: the Anthropic Models API, the Gemini
  `models.get` endpoint, or the models installed in Ollama (`GET /api/tags`).

A check result is cached for `LLM_CATALOG_TTL_SECONDS` (300). A check that times out
(`LLM_CATALOG_CHECK_TIMEOUT_SECONDS`, 5) or fails for another reason makes the model's
availability `unknown`, which still allows asking; "unreachable" and "unknown" results are
re-checked after 30 seconds. `GET /chat/models?refresh=true` checks again, at most every 10
seconds.

**Selection rules** (enforced on the server for every question):

| Request | Result |
| --- | --- |
| No `model` | The default (`LLM_PROVIDER` / `LLM_MODEL`); `503 llm_not_configured` if that provider has no credentials. Another configured provider is **not** used instead |
| A provider or model that is not in the server's configuration | `422 invalid_model`; the client's text is not echoed back |
| A listed model of a provider without credentials | `503 provider_not_configured` |
| A listed model the provider reported as unavailable | `503 model_unavailable`, with the reason |
| A listed, configured model | Answered by exactly that provider and model |

The browser can only name a provider and a model ID, both pattern-checked, and never a URL,
key or endpoint. If the chosen model fails while answering (rate limit, outage, timeout),
the failure is reported with a Retry button. **The server never switches to another
provider or model.** In particular, a free or local model that fails is never silently
replaced by a paid one. The only substitution that exists is Anthropic's own server-side
fallback for policy refusals (`LLM_FALLBACKS`, Claude to Claude), which is recorded on the
answer and shown under it.

Each answer stores the **requested** provider and model (what the user chose,
`requested_provider` / `requested_model`), the provider and model the server called
(`provider` / `model`), and the model that actually answered (`served_model`, which differs
only after a fallback). Users can change models between any two questions of a
conversation; history and citations work the same across models.

## Configuration

Shared settings:

| Variable | Default | Meaning |
| --- | --- | --- |
| `LLM_PROVIDER` | `anthropic` | Default provider: `anthropic`, `gemini` or `ollama` |
| `LLM_MODEL` | unset | Default model; unset means the first model in that provider's list. A model named only here is added to the front of the list |
| `LLM_MAX_OUTPUT_TOKENS` | `2048` | Longest answer; longer answers stop with `stop_reason: max_tokens` |
| `LLM_TEMPERATURE` | unset | Sent to every provider when set. Current Claude models reject it, so leave it unset if Claude is configured |
| `LLM_CONNECT_TIMEOUT_SECONDS` | `10` | Connection timeout |
| `LLM_READ_TIMEOUT_SECONDS` | `60` | Longest wait for the next piece of a streamed response (Gemini: the SDK's per-request timeout) |
| `LLM_REQUEST_TIMEOUT_SECONDS` | `180` | Deadline for one whole answer, retries included |
| `LLM_MAX_RETRIES` | `2` | Retries for connection errors, 408, 429 and 5xx before any text has streamed |
| `LLM_CATALOG_TTL_SECONDS` | `300` | How long an availability check is reused |
| `LLM_CATALOG_CHECK_TIMEOUT_SECONDS` | `5` | Timeout of one availability check |

Model lists are comma-separated, de-duplicated, at most 20 per provider, and each ID must
match `^[A-Za-z0-9][A-Za-z0-9._:/@+-]{0,127}$`. Invalid settings stop the server at startup.

**Model IDs are configuration, not code.** The defaults below are the current models listed
in each provider's official model documentation on 2026-10-13. Providers retire models, so
check the linked pages and override the lists when they change. A configured model the
provider no longer offers shows as unavailable in the selector (with the reason) and cannot
be chosen; it is never replaced by another model.

### Anthropic Claude

| Variable | Default | Meaning |
| --- | --- | --- |
| `ANTHROPIC_API_KEY` | unset | Server-side only. Without it Claude models are listed as not configured |
| `ANTHROPIC_MODELS` | `claude-opus-5-5,claude-sonnet-5-5,claude-haiku-5-5` | Models offered ([Anthropic models overview](https://docs.claude.com/en/docs/about-claude/models/overview)) |
| `LLM_EFFORT` | unset | `low` … `max`, sent as `output_config.effort`; unset uses the model's default |
| `LLM_FALLBACKS` | `default` | Server-side fallback when the requested model declines a request on policy grounds (`fallbacks: "default"` with the `server-side-fallback-2026-07-01` beta); `none` turns it off. The served model is recorded and shown |

The SDK retries connection errors, 408, 409, 429 and 5xx (honouring `retry-after`) before
any text has streamed. Errors are classified by the SDK's exception class and, for errors
inside an HTTP 200 stream, by their `type`. The SDK also honours its standard
`ANTHROPIC_BASE_URL` variable (for example for a gateway). Availability is checked with
`models.retrieve` for each listed model.

### Google Gemini

| Variable | Default | Meaning |
| --- | --- | --- |
| `GEMINI_API_KEY` | unset | Server-side only, sent in the `x-goog-api-key` header by the SDK |
| `GEMINI_MODELS` | `gemini-3.8-flash,gemini-3.5-flash-lite` | Flash and Flash-Lite ([Gemini models](https://ai.google.dev/gemini-api/docs/models)) |
| `GEMINI_TIER` | `free` | Labels the models "Free tier" or "Paid" and picks the notice. Google decides the real tier from the key's project billing; set this to match |
| `GEMINI_THINKING_LEVEL` | unset | `minimal`, `low`, `medium`, `high`; unset uses the model's default |

The adapter uses `client.aio.models.generate_content_stream` with the system prompt as
`system_instruction`, `max_output_tokens`, and one candidate. Details:

- **Retries** are the SDK's (`HttpRetryOptions`) for 408, 429, 500, 502, 503 and 504,
  before any text has streamed.
- **Rate limits** (`429 RESOURCE_EXHAUSTED`) become `provider_rate_limited` with the
  `retryDelay` Google returns, so the UI can say when to try again. On the free tier this
  is the error users will see most.
- **Errors**: invalid key → authentication; 403 and `FAILED_PRECONDITION` (for example the
  free tier is not available in the key's region) → permission; 404 → model not found;
  413 → too large; 503 → overloaded; 504 → timeout.
- **Safety blocks**: a blocked prompt (`promptFeedback.blockReason`) or a `SAFETY`-style
  finish reason is treated as a refusal (`model_refused`).
- **Usage**: input tokens are `promptTokenCount` minus `cachedContentTokenCount` (cached
  tokens are recorded separately); output tokens are `candidatesTokenCount` plus
  `thoughtsTokenCount`, because thinking tokens are billed as output even though they are
  dropped.
- **Served model** comes from the response's `modelVersion`.

### Ollama

| Variable | Default | Meaning |
| --- | --- | --- |
| `OLLAMA_BASE_URL` | unset | For example `http://localhost:11434`, or `http://host.docker.internal:11434` from Docker. `http`/`https` only, no credentials, query or fragment |
| `OLLAMA_MODELS` | empty | Allowlist, for example `qwen3:8b,gemma3:12b`. A name without a tag matches `:latest` |
| `OLLAMA_NUM_CTX` | `16384` | Context window requested per call. Ollama's own default (4096) is smaller than the chat prompt can be, and Ollama silently drops the start of a prompt that does not fit |
| `OLLAMA_THINK` | unset | Ollama's `think` option; unset leaves the model's default |
| `OLLAMA_KEEP_ALIVE` | unset | How long the model stays loaded after a request, e.g. `5m`, `1h`, `-1` |
| `OLLAMA_ALLOW_REMOTE_MODELS` | `false` | Ollama "cloud" models run on ollama.com, not locally; they are refused unless this is true |

Ollama is configured only when both a base URL and at least one model are set. The adapter
posts to `/api/chat` with `stream: true` and reads newline-delimited JSON until the final
`done: true` chunk (`done_reason` `stop` or `length`, `prompt_eval_count`, `eval_count`).
It retries connection errors and HTTP 408, 429, 500, 502 and 503 before any text has
streamed (0.5 s doubling, or `Retry-After` up to 30 s), does not follow redirects, and
closes the HTTP stream when the user stops. A 404 means the model is not installed ("run
`ollama pull`"); an `{"error": …}` line mid-stream, invalid JSON or a stream without the
final chunk are reported as errors. The catalog lists only allowlisted models that
`/api/tags` shows as installed. Error messages never include the configured URL.

## Getting a Gemini API key

1. Sign in to [Google AI Studio](https://aistudio.google.com/) with a Google account.
2. Open **Get API key** (<https://aistudio.google.com/app/apikey>) and create a key. AI
   Studio creates or uses a Google Cloud project for it. Keys in a project without billing
   use the free tier.
3. Put it in `.env` on the server, never in the frontend:

   ```bash
   GEMINI_API_KEY=your-key
   GEMINI_TIER=free        # or paid, if billing is enabled on the key's project
   ```

4. Restart the API. The Chat page's model selector then lists the Gemini models, and the
   server checks that the key can see them.

Treat the key like a password: anyone with it can spend the project's quota. To stop
using it, delete it in AI Studio.

### Free-tier limits and data handling

- **Limits.** The free tier has low per-model limits on requests per minute, tokens per
  minute and requests per day, set per project and changed by Google from time to time;
  current values are on the [rate limits page](https://ai.google.dev/gemini-api/docs/rate-limits)
  and in AI Studio. A RAG answer sends several thousand tokens of passages per question
  (`CHAT_CONTEXT_TOKEN_BUDGET`, 8000 by default), so token limits are reached sooner than
  the question count suggests. When a limit is hit, the answer fails with "rate limited",
  the time Google suggests waiting, and a Retry button; the server does not switch to
  another model. Some models or regions may have no free tier at all.
- **Data use.** Under the [Gemini API Additional Terms](https://ai.google.dev/gemini-api/terms),
  content sent to the free ("unpaid") service may be used by Google to provide, improve
  and develop its products, and may be read by human reviewers. Every question sends your
  question, recent conversation history and passages from your documents. **Do not use
  the free tier for confidential, personal or customer documents.** On the paid tier,
  Google states that prompts and responses are not used to improve its products. Regional
  rules can differ; read the terms for your location. The selector shows this warning
  next to free-tier models.
- **Paid tier** bills each question to the project; the model is labelled "Paid".

## Installing Ollama on macOS

1. Install Ollama: download the app from <https://ollama.com/download> and move it to
   Applications (or `brew install ollama`), then start it. It serves on
   `http://localhost:11434`; check with `curl http://localhost:11434/api/version`.
2. Download one or more models. Pick sizes that fit your Mac's memory: a model needs
   roughly its download size in free RAM, plus room for the context window. For example:

   ```bash
   ollama pull qwen3:8b       # about 5 GB
   ollama pull gemma3:12b     # about 8 GB
   ollama list                # the exact names to put in OLLAMA_MODELS
   ```

   Choose models from the [Ollama library](https://ollama.com/library) that handle both
   Japanese and English. Small models follow the citation instructions less reliably.
3. Configure the API (in `.env`):

   ```bash
   OLLAMA_MODELS=qwen3:8b,gemma3:12b
   # API in Docker Compose (Docker Desktop for Mac): this is the Compose default.
   OLLAMA_BASE_URL=http://host.docker.internal:11434
   # API running directly on the Mac (README Option B):
   # OLLAMA_BASE_URL=http://localhost:11434
   ```

4. Restart the API and open the Chat page. If Ollama is not running, its models show as
   unavailable with the reason; start Ollama and press **Check again**.

From Docker Desktop for Mac, `host.docker.internal` reaches services on the Mac, including
Ollama's default `127.0.0.1:11434`, so Ollama does not need to listen on other interfaces.
If your setup cannot reach it (for example a different container runtime), make Ollama
listen on all interfaces with `launchctl setenv OLLAMA_HOST "0.0.0.0:11434"` and restart
the Ollama app. That also exposes it to your local network, without authentication, so only
do it on a trusted network or firewall the port. On Linux, Compose maps
`host.docker.internal` to the host gateway, and Ollama must listen on an address the
container can reach (for example `OLLAMA_HOST=0.0.0.0:11434` in its systemd unit).

**Data handling:** prompts and documents go only to the Ollama server at
`OLLAMA_BASE_URL`. Ollama's "cloud" models (usually named with `-cloud`; the server
detects them by the `remote_host` in their `/api/tags` entry) send prompts to ollama.com;
they are refused unless
`OLLAMA_ALLOW_REMOTE_MODELS=true`, so a "Local" label is accurate. Running models locally
has no per-question cost, but answers are slower, especially on long RAG prompts, and
usually less accurate than hosted models.

## Adding another provider

A new provider is an `LLMProvider` (its `stream()` and error mapping), a `ProviderBackend`
(configuration, cost class, notice, availability check) and its settings, registered in
`create_llm_registry` ([`backend/app/llm/__init__.py`](../backend/app/llm/__init__.py)).
The prompt uses no provider-specific features, so the chat pipeline, persistence, SSE
protocol and UI do not change. Prompt behaviour (citation rate, abstention) should be
measured with the evaluation harness for each new model.

There is deliberately **no stand-in provider** in the application: without credentials, a
provider is unavailable rather than answered by something that is not a model. The
test-only `ScriptedProvider` and `FakeBackend` (`backend/tests/chat_fakes.py`) and the
evaluation-only `ExtractiveProvider` (`backend/evaluation/extractive.py`) cannot be
selected from the app configuration.

## Verification status

- **Verified with test doubles (CI):** each provider is driven through its real client
  against a mocked HTTP transport that returns the provider's documented wire format:
  - Anthropic (`tests/test_llm_anthropic.py`): the real `anthropic` SDK against
    `httpx2.MockTransport` Server-Sent Events: request parameters, streaming, usage, stop
    reasons, fallback, every error class, SDK retries, early close, availability checks,
    and that the key never appears in logs.
  - Gemini (`tests/test_llm_gemini.py`): the real `google-genai` SDK against an
    `httpx.MockTransport` serving `streamGenerateContent?alt=sse`: the request URL, key
    header and body, streaming text with thought parts dropped, usage, stop reasons,
    safety blocks, rate limits with `retryDelay`, every error mapping, SDK retries before
    streaming, no retry after streaming, early close, availability checks, and no key
    leakage.
  - Ollama (`tests/test_llm_ollama.py`): the real adapter against `httpx2.MockTransport`
    NDJSON: request body and options, streaming, usage, `done_reason`, mid-stream errors,
    malformed streams, retries with `Retry-After`, unreachable server, early close, the
    `/api/tags` availability rules (tags, not installed, cloud models), and URL validation.
  - The registry, selection rules, catalog endpoint, model switching within a
    conversation, persistence of requested and actual models, cancellation, retry with a
    different model and no-fallback behaviour (`tests/test_llm_registry.py`,
    `tests/test_chat_models_api.py`), plus the Phase 4 chat tests unchanged.
- **Not verified:** a request to the real Anthropic, Gemini or Ollama services. The
  development environment has no API keys and no Ollama server, so **no real-provider
  request was made**. The model IDs were checked against the providers' documentation, not
  against the live APIs. How to run the optional real-provider smoke tests yourself:
  [llm-smoke-tests.md](llm-smoke-tests.md).
