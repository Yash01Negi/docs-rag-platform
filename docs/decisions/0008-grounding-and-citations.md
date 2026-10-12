# ADR 0008: Numbered source passages, server-checked citation markers, and stored citations

- **Status:** Accepted
- **Date:** 2026-10-12

## Context

Answers must be grounded in the uploaded documents, cite the passages they use, never cite
something that was not retrieved, say so when the documents do not answer, and resist
instructions planted in documents. Citations must still make sense after a document is
reindexed or deleted.

## Options considered

1. **Provider-native citations** (for example Anthropic's document citations). Precise
   character ranges, but provider-specific, and they would tie the stored format and the UI
   to one provider.
2. **Structured output** (JSON with answer and citation lists). Easy to validate, but it
   cannot be streamed to the user as readable text.
3. **Inline markers in the text (`[S1]`), checked by the server while streaming.** Works
   with any model, streams naturally, and is easy to render.

## Decision

Option 3.

- Retrieved passages are numbered `S1..Sn` and sent as escaped `<source>` elements; the
  system prompt says to cite every statement, to begin with `[NOT_FOUND]` when the
  passages do not answer, and to treat passage text as untrusted data.
- A streaming filter checks every marker against the passages actually given, removes and
  counts invalid ones, normalises valid ones, and records an abstention; it holds back at
  most 64 characters while a marker might be incomplete. Invalid citation references never
  reach the client or the database.
- If retrieval finds nothing, the model is not called and a fixed notice is the answer.
- Every passage given to the model is stored as a citation row with its chunk ID and
  metadata (document, filename, chunk position, pages, section, language, rank, score, a
  text hash and an excerpt), flagged `supporting` when the answer cites it. Chunk and
  document IDs are not foreign keys, so answers survive reindexing and deletion; when read,
  each citation is located again (`available`, `relocated` by text hash, `changed`,
  `deleted`). Deleting a document clears its excerpts.

## Consequences

- Any model that follows the instructions can be used, and the format is the same for
  streamed and stored answers.
- The server guarantees that a citation points at a retrieved passage, but not that the
  passage supports the sentence; that is measured (citation precision in
  [evaluation/chat.md](../evaluation/chat.md)), not enforced.
- Citations are at passage (chunk) granularity, not character ranges.
- A model that ignores the format produces `uncited` answers, which the UI flags.
