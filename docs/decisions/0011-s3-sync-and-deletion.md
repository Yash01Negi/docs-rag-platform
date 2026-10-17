# ADR 0011: S3 connector sync and deletion semantics

- **Status:** Accepted
- **Date:** 2026-10-17
- **Builds on:** [ADR 0004](0004-document-storage.md) (document storage), the ingestion job
  queue ([ingestion-pipeline.md](../ingestion-pipeline.md))

## Context

Phase 5 imports documents from Amazon S3 buckets and keeps them in sync automatically. The
sync must be incremental (no re-downloading unchanged objects), must never show a document
half old and half new, must not lose documents because of a transient S3 failure, must
survive crashes and restarts, and must work with several workers without Celery or Redis.
S3 ETags are not reliable content hashes: multipart uploads and SSE-KMS produce ETags that
are not MD5 sums. Manual uploads must never be affected by a connector, and AWS credentials
must not reach the browser or the database.

## Decision

1. **Sync runs are database jobs.** `connector_sync_runs` uses the ingestion queue's lease
   protocol: claim with `FOR UPDATE SKIP LOCKED`, a fresh `lease_token` per claim, lease
   renewal, and recovery of expired leases. A partial unique index allows one queued or
   running run per connector, so a manual sync, the scheduler and several workers can race
   safely (`INSERT … ON CONFLICT DO NOTHING`). Every write of a run first locks the run row
   and checks the token, so a worker that lost its lease cannot commit.
2. **The worker schedules.** Due connectors (`next_sync_at <= now()`, database time) are
   queued by whichever worker polls first. Scheduling therefore survives restarts and does
   not depend on a browser or on a single scheduler process.
3. **Change detection compares ETag and size as opaque values.** An object is unchanged
   when both match the version last imported. No MD5 is computed or assumed. The version ID
   is recorded for display and audit, not used for detection (it is absent on unversioned
   buckets).
4. **Downloads are pinned to the listed version** with `If-Match`, and capped at
   `S3_MAX_OBJECT_BYTES`, so the bytes indexed are exactly the bytes listed.
5. **One object, one transaction.** Storing a new or changed object, deleting a changed
   document's old chunks, and queueing its ingestion job commit together. Search sees the old
   revision, then nothing, then the new revision; never a mix. A document that is still
   being indexed is not changed under the running job (`document_busy`, retried next run).
6. **Deletion requires a complete listing.** Documents are removed only after every page was
   listed without error, within the object limit, and the run was not cancelled. Any listing
   failure fails the run and deletes nothing. Removal reuses the normal document deletion
   path, and the `connector_objects` row stays as an audit record (`status = 'deleted'`).
7. **Ownership is explicit.** A document records its `source_type`; only documents a
   connector imported (linked through `connector_objects`) can be updated or removed by it.
   Content de-duplication now applies to uploads only, so an uploaded file and an imported
   object with identical bytes are separate documents. Overlapping connectors are refused so
   every object belongs to one connector, and a connector's bucket and prefix are immutable.
8. **Rejected versions are remembered.** An object version that cannot be imported (too
   large, invalid content) is recorded as `failed` with its ETag and not downloaded again
   until it changes. Transient object errors are retried on the next run.
9. **Credentials stay in the worker's environment.** boto3's default chain (environment,
   profile, IAM role); nothing in PostgreSQL, nothing in the API. The only endpoint override
   is the server-side `S3_ENDPOINT_URL`, validated to `scheme://host[:port]`. A server-side
   allowlist (`S3_ALLOWED_SOURCES`) bounds which buckets and prefixes can be used, and the
   worker re-checks it on every run.

## Alternatives considered

- **S3 event notifications (SQS or EventBridge).** Faster, but needs AWS infrastructure the
  project does not have, and a periodic full listing would still be needed to repair missed
  events. Deferred.
- **Content hashing for change detection** (download and hash every object). Correct but
  downloads everything on every sync. ETag plus size is the documented S3 way to detect a
  new version; a false "changed" only costs a re-index.
- **Keep the old chunks until the new revision is indexed, then swap.** Avoids the short gap
  in searchability, but needs two chunk sets per document and a swap step in the ingestion
  pipeline. Rejected for now; the gap is seconds to minutes for typical documents.
- **Soft-delete documents whose objects disappear.** Keeps them recoverable, but they would
  still need to be hidden from search and chat everywhere. The audit row records what was
  removed; the object itself (or its S3 version history) is the source of truth.
- **Celery/Redis or a separate scheduler service.** Rejected: PostgreSQL already provides
  the queue, leases and locking, as for ingestion.

## Consequences

- A sync is idempotent and safe to repeat; crashes cost at most one object's work.
- A transient outage never deletes documents, but deletions in S3 can take until the next
  successful sync to show up.
- While a changed document is re-indexed it is briefly missing from search results.
- Every sync lists the whole prefix: cost grows with the number of objects, bounded by
  `S3_MAX_OBJECTS_PER_SYNC`.
- Deleting an imported document by hand is temporary: the next sync imports it again.
- The migration (`0006_connectors`) cannot be downgraded while imported documents exist.
