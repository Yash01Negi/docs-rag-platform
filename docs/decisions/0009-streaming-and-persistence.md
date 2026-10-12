# ADR 0009: Server-Sent Events over POST, with the answer stored as it is generated

- **Status:** Accepted
- **Date:** 2026-10-12

## Context

Answers take seconds to generate and should appear as they are written. The client must be
able to stop an answer, a dropped connection must not leave the database inconsistent, and a
retry must not store a question or answer twice.

## Options considered

- **WebSockets:** bidirectional, but a second protocol, harder to put behind ordinary
  proxies, and nothing in the chat needs messages from the client mid-answer.
- **`EventSource` (GET) SSE:** cannot send a JSON body or custom headers.
- **SSE as the response of a `POST` (read with `fetch` and a stream reader):** one request
  per question, plain HTTP, works through proxies, and closing the request stops the answer.

## Decision

- `POST /conversations/{id}/messages` returns `text/event-stream` with named events
  (`start`, `sources`, `delta`, `citations`, `usage`, `done`, `error`), numbered `id:`s,
  one JSON `data:` line per event, and keep-alive comments ([chat-api.md](../chat-api.md)).
  Errors found before streaming are normal JSON error responses.
- The pipeline runs in its own task; the HTTP response only relays its events. A client
  disconnect cancels the task, which stores the partial answer as `cancelled` (the write is
  shielded from the cancellation).
- Two short transactions: before generation (lock the conversation row, store the question
  and an empty `streaming` answer) and after (update the answer only if it is still
  `streaming`, insert citations). No transaction is held open while the model runs.
  Unique indexes (one of them partial) allow one streaming answer per conversation, one answer per
  question and one question per client idempotency key.
- A client-generated `client_message_id` makes retries idempotent: a completed answer is
  replayed as an event stream, a failed or stopped one is regenerated in place.

## Consequences

- Browsers must use `fetch` streaming rather than `EventSource`; the frontend has a small
  SSE parser.
- An answer interrupted by a server crash stays `streaming` until it is old enough
  (request timeout plus two minutes) to be expired; until then the conversation answers
  `409`.
- The per-process limit on concurrent answers is not shared between API processes; the
  database constraints are.
