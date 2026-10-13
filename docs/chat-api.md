# Chat API and streaming protocol

Phase 4 adds conversations: questions about the uploaded documents, answered by a language
model from retrieved passages, with citations that point at those passages. All endpoints
are under `/api/v1`. Interactive schemas are at `/docs` (OpenAPI) when the API runs.

> **No authentication.** Anyone who can reach the API can list, read, rename and delete
> every conversation, and read every question and answer. Conversations are not private
> between users. See [Security and limitations](chat.md#security-and-limitations).

## Endpoints

| Method and path | Purpose |
| --- | --- |
| `GET /chat/options` | Whether answering is configured, the default provider and model, and the limits clients validate against |
| `GET /chat/models?refresh=false` | The providers and models this server offers, with cost class and availability ([below](#choosing-a-model)) |
| `POST /conversations` | Start a conversation (`{"title": "…"}` optional; otherwise the first question becomes the title). `201` |
| `GET /conversations?limit=50&offset=0` | Conversations, most recently active first, with message counts |
| `GET /conversations/{id}` | One conversation, its messages (oldest first, latest 500) and each answer's citations |
| `PATCH /conversations/{id}` | Rename (`{"title": "…"}`, 1 to 200 characters) |
| `DELETE /conversations/{id}` | Delete the conversation, its messages and citations. `204` |
| `POST /conversations/{id}/messages` | Ask a question; the response is a Server-Sent Events stream |

Errors use the application's usual envelope:

```json
{"error": {"code": "conversation_busy", "message": "…", "request_id": "…"}}
```

### Asking a question

```http
POST /api/v1/conversations/{id}/messages
Content-Type: application/json
Accept: text/event-stream

{
  "content": "在宅勤務は週に何日までできますか？",
  "client_message_id": "8f0c…",          // optional idempotency key (UUID), recommended
  "retrieval": {                          // optional
    "mode": "semantic",                   // semantic | keyword | hybrid
    "document_ids": ["…"],                // only search these documents (1 to 100)
    "language": "ja"                      // only passages in this language: ja | en | mixed
  },
  "model": {                              // optional; omitted: the server default
    "provider": "gemini",                 // a provider name from GET /chat/models
    "model": "gemini-3.8-flash"           // one of that provider's listed models
  }
}
```

The question is NFC-normalized, control characters other than tab and newline are removed,
and it must be 1 to `CHAT_MAX_QUESTION_CHARS` (default 4,000) characters. Unknown fields
are rejected.

**Errors before the stream starts** are ordinary JSON responses, and nothing is stored:

| Status | Code | When |
| --- | --- | --- |
| 404 | `conversation_not_found` | The conversation does not exist |
| 409 | `conversation_busy` | An answer is still being generated in this conversation (one at a time) |
| 409 | `client_message_id_reused` | The key was used before for a different question |
| 422 | `validation_error`, `question_too_long`, `unknown_document` | Invalid input |
| 422 | `invalid_model` | `model` names a provider or model the server does not offer |
| 429 | `too_many_generations` | `CHAT_MAX_CONCURRENT_GENERATIONS` answers are running in this process; `Retry-After: 5` |
| 503 | `llm_not_configured` | No `model` was sent and the default provider has no credentials (for Claude: no `ANTHROPIC_API_KEY`); reading and managing conversations still works |
| 503 | `provider_not_configured` | The chosen provider has no credentials (or, for Ollama, no base URL or models) |
| 503 | `model_unavailable` | The provider reported the chosen model as unavailable (not offered, not installed, unreachable); the message says why |
| 503 | `database_unavailable` | The database cannot be reached |

Once the response is `200 text/event-stream`, every later failure arrives as an `error`
event, and the stream then ends.

### Choosing a model

`GET /chat/models` lists what the server offers. It is built from server configuration
only and never contains API keys or provider URLs:

```json
{
  "providers": [
    {"name": "anthropic", "label": "Anthropic Claude", "cost": "paid", "configured": false,
     "reason": "ANTHROPIC_API_KEY is not set", "notice": "Paid API: …"},
    {"name": "gemini", "label": "Google Gemini", "cost": "free_tier", "configured": true,
     "reason": null, "notice": "Free tier: low rate limits, and Google may use …"},
    {"name": "ollama", "label": "Ollama (local)", "cost": "local", "configured": true, "reason": null,
     "notice": "Runs on the Ollama server …"}
  ],
  "models": [
    {"provider": "gemini", "provider_label": "Google Gemini", "model": "gemini-3.8-flash",
     "cost": "free_tier", "configured": true,
     "availability": {"state": "available", "reason": null},
     "selectable": true, "default": false},
    {"provider": "ollama", "provider_label": "Ollama (local)", "model": "gemma3:12b",
     "cost": "local", "configured": true,
     "availability": {"state": "unavailable", "reason": "Not installed in Ollama. Run `ollama pull gemma3:12b` on the Ollama host."},
     "selectable": false, "default": false}
  ],
  "default": {"provider": "anthropic", "model": "claude-opus-5-5"}
}
```

- `cost` is `paid`, `free_tier` or `local`.
- `availability.state` is `available` (the provider confirmed it), `unavailable` (with a
  reason) or `unknown` (not checked yet, or the check failed; asking may still work).
- `selectable` is `configured` and not `unavailable`. Only selectable models are accepted.
- Results are cached for `LLM_CATALOG_TTL_SECONDS`; `?refresh=true` checks again (at most
  every 10 seconds).

Send the chosen pair as `model` with a question. The server validates it against the
catalog every time and answers with exactly that model or an error; it never substitutes
another provider or model, and a failure mid-answer is reported, not retried elsewhere.
`model` may change from one question to the next in the same conversation.

### Retries and idempotency

Send a fresh `client_message_id` with each new question and reuse it when retrying the same
question (after a network error, or with the Retry button):

- If that question already has a **complete** answer, the stored answer is replayed as a
  normal event stream (`start.replayed` is `true`), whatever `model` the retry names. The
  model is not called again and nothing is stored twice.
- If its answer **failed or was stopped**, a new answer is generated and replaces the old
  one in the same position. The question is not stored again. The retry may name a
  different `model` (for example after a rate limit); the new answer records it.
- The same key with different question text is rejected (`409 client_message_id_reused`).

Without a key, every request stores a new question.

## Event stream

The stream follows the [Server-Sent Events](https://html.spec.whatwg.org/multipage/server-sent-events.html)
format. Each event has an `id:` (1, 2, 3, … within the response), an `event:` name and one
`data:` line holding a JSON object, and ends with a blank line. Lines starting with `:` are
comments: the stream opens with `: stream open`, and `: keep-alive` is sent after
`CHAT_HEARTBEAT_SECONDS` (default 15) without an event, so proxies keep the connection
open. Responses carry `Cache-Control: no-cache` and `X-Accel-Buffering: no`.

```text
: stream open

id: 1
event: start
data: {"request_id":"6f1e…","conversation_id":"…","user_message_id":"…","assistant_message_id":"…","provider":"anthropic","model":"claude-opus-5-5","replayed":false}

id: 2
event: sources
data: {"retrieval":{"mode":"semantic","semantic_available":true,"warnings":[],"duration_ms":182.4},"sources":[{"index":1,"chunk_id":"…","document_id":"…","filename":"ja-remote-work.md","chunk_ordinal":0,"page_start":null,"page_end":null,"section":"在宅勤務の上限","language":"ja","retrieval_rank":1,"score":0.71,"score_type":"cosine_similarity","excerpt":"# 在宅勤務規程…"}]}

id: 3
event: delta
data: {"text":"在宅勤務は原則として週3日までです"}

id: 4
event: delta
data: {"text":"[S1]。"}

id: 5
event: citations
data: {"citations":[{"index":1,"supporting":true,"chunk_id":"…", …}],"invalid_markers_removed":0}

id: 6
event: usage
data: {"input_tokens":1834,"output_tokens":41,"cache_read_input_tokens":0,"cache_creation_input_tokens":0}

id: 7
event: done
data: {"assistant_message_id":"…","status":"complete","grounding":"grounded","stop_reason":"end_turn","served_model":"claude-opus-5-5","timings_ms":{"total_ms":2311.2,"retrieval_ms":182.4,"first_token_ms":1104.9}}
```

### Events

Events arrive in this order: `start`, `sources`, any number of `delta`, then either
`citations`, optionally `usage`, and `done`; or `error`. An `error` can come at any point
after `start` (or as the only event, in the rare case described below).

| Event | Data | Notes |
| --- | --- | --- |
| `start` | `request_id`, `conversation_id`, `user_message_id`, `assistant_message_id`, `provider`, `model`, `replayed` | The question and an empty answer (status `streaming`) are stored before this event. `provider` and `model` are the ones answering (the chosen model, or the default) |
| `sources` | `retrieval` {`mode`, `semantic_available`, `warnings`, `duration_ms`}, `sources` [Source] | Every passage given to the model, numbered `index` 1..n. If semantic search is unavailable the sources come from keyword search and a warning says so |
| `delta` | `text` | The next piece of the answer (Markdown). Citation markers in it are already checked: see below |
| `citations` | `citations` [Source + `supporting`], `invalid_markers_removed` | Every source again, with `supporting: true` for those the answer cites and `false` for context that was given but not cited |
| `usage` | `input_tokens`, `output_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens` | Reported by the provider. Absent when the model was not called |
| `done` | `assistant_message_id`, `status` (`complete`), `grounding`, `stop_reason`, `served_model`, `timings_ms` | The answer is stored. `served_model` can differ from `model` when the provider fell back to another model |
| `error` | `code`, `message`, `retryable`, `retry_after_seconds`, `assistant_message_id`, `request_id` | The stream ends after it. `message` is safe to show; it never contains provider error text or credentials |

**Source** objects: `index`, `chunk_id`, `document_id`, `filename`, `chunk_ordinal`,
`page_start`, `page_end`, `section`, `language`, `retrieval_rank` (1 = best),
`score`, `score_type` (`cosine_similarity`, `bm25` or `rrf`, as in search), `excerpt` (the
first `CHAT_CITATION_EXCERPT_CHARS` characters of the passage).

**Grounding** (`done.grounding`, also stored on the message):

| Value | Meaning |
| --- | --- |
| `grounded` | The answer cites at least one source |
| `insufficient_evidence` | The model said the sources do not answer the question (it began with `[NOT_FOUND]`, which is removed from the text) |
| `no_sources` | Retrieval found nothing; the model was **not called** and a fixed notice (Japanese or English, after the question) is the answer |
| `uncited` | The model answered without citing anything; the UI warns the reader |

**`stop_reason`** is the provider's own value (for Anthropic `end_turn`, `max_tokens`,
`refusal`, …). `max_tokens` means the answer was cut at `LLM_MAX_OUTPUT_TOKENS`; it is
still stored as complete. A `refusal` is stored as failed with code `model_refused`.

**Error codes inside the stream:**

| Code | Retryable | Meaning |
| --- | --- | --- |
| `provider_rate_limited` | yes | The provider rate limit was hit (`retry_after_seconds` when known) |
| `provider_overloaded` | yes | The provider is overloaded (HTTP 529) |
| `provider_timeout` | yes | No complete answer within `LLM_REQUEST_TIMEOUT_SECONDS`, or a read timeout |
| `provider_connection_error` | yes | Network failure reaching the provider |
| `provider_server_error` | yes | 5xx from the provider |
| `provider_invalid_response` | yes | The response stream ended without a final event |
| `provider_authentication_failed`, `provider_permission_denied` | no | The server's key is wrong or lacks access |
| `provider_bad_request`, `provider_request_too_large`, `provider_model_not_found` | no | Configuration or request problem on the server |
| `model_refused` | no | The model declined to answer |
| `internal_error` | yes | An unexpected server error |
| `conversation_busy`, `already_answered`, `too_many_generations`, `llm_not_configured`, `provider_not_configured`, `model_unavailable`, `invalid_model`, `conversation_not_found` | varies | A race the pre-stream check could not see (another request got there first); these come as the only event, before `start` |

Each provider retries connection errors, 408, 429 and 5xx (Claude also 409) up to
`LLM_MAX_RETRIES` times before any text has streamed. A failure is never retried with a
different provider or model. After text has streamed, a failure is not retried: the partial
text is kept, the answer is stored as `failed`, and the client can retry with the same
`client_message_id`.

### Citation markers

The model cites passages as `[S1]`, `[S1][S3]` or `[S1, S3]` (full-width `［S1］`,
`【S1】` and the separators `，` and `、` are accepted). The server checks every marker before
it reaches the client:

- a marker naming a source that was given is normalized to `[S1][S3]`;
- a marker naming any other number is **removed** and counted in
  `invalid_markers_removed`, so every marker a client receives maps to a real chunk;
- `[NOT_FOUND]` is removed and sets `grounding` to `insufficient_evidence`.

Because a marker can be split between two provider chunks, text from a `[` is held back
until it is clearly a marker or clearly not one (at most 64 characters). Clients can
render `[S<n>]` as a link to the source with `index` n of the same answer.

### Stopping and disconnects

Closing the connection (for example `AbortController.abort()` in the browser) stops the
answer: the server cancels the provider request, and stores what had streamed so far with
status `cancelled` (error code `cancelled`). The answer is stored even though the client
is gone. Retrying with the same `client_message_id` generates a new answer in its place.

If the server process stops while answering, the answer stays `streaming` in the database
and blocks new questions in that conversation (`409 conversation_busy`) until it is older
than `LLM_REQUEST_TIMEOUT_SECONDS` + 120 seconds. After that it no longer blocks, and the
next question marks it `failed` (`interrupted`). Deleting a conversation while it is answering is
allowed; the stream ends normally and nothing more is stored.

At shutdown the API waits up to 10 seconds for running answers, then cancels them (stored
as `cancelled`).

## Stored conversations

`GET /conversations/{id}` returns messages with: `id`, `sequence`, `role` (`user` or
`assistant`), `content` (answers keep their checked `[S<n>]` markers), `status`
(`complete`, `streaming`, `failed`, `cancelled`), `created_at`, `completed_at`,
`client_message_id`, `reply_to_id` (the question an answer belongs to), `grounding`,
`error_code`, `error_message`, `requested_provider`, `requested_model` (what the user
chose), `provider`, `model` (what the server called), `served_model` (what answered),
`stop_reason`, `usage`,
`retrieval_mode`, `timings_ms` and `citations`.

Each stored citation is a Source with `supporting` plus where the passage is **now**:

| `source_status` | Meaning | `current_chunk_id` / `current_chunk_ordinal` |
| --- | --- | --- |
| `available` | The chunk still exists | the same chunk |
| `relocated` | The document was reindexed; a chunk with exactly the same text exists | the new chunk |
| `changed` | The document exists but no chunk has this text any more | null |
| `deleted` | The document was deleted (`excerpt` is then null too) | null |

The UI links `available` and `relocated` citations to
`/documents/{document_id}?chunk={ordinal}`, which opens the document at that passage.
