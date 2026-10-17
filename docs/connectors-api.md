# Connectors API

Phase 5 endpoints for S3 connectors and their sync runs. All paths are under `/api/v1`;
interactive schemas are at `/api/v1/docs` when the API runs. How syncs behave is described
in [connectors.md](connectors.md).

> **No authentication**, like the rest of the API. No endpoint accepts or returns AWS
> credentials or endpoint URLs.

## Endpoints

| Method and path | Purpose |
| --- | --- |
| `GET /connectors/options` | Whether connectors are configured, the allowed sources and the limits the form validates against |
| `GET /connectors?limit=20&offset=0` | Connectors, sorted by name, with their document counts |
| `POST /connectors` | Create a connector. `201`; the first sync is scheduled immediately when `enabled` |
| `GET /connectors/{id}` | One connector with its active and latest run and object counts |
| `PATCH /connectors/{id}` | Change `name`, `sync_interval_minutes` or `enabled` |
| `DELETE /connectors/{id}` | Delete the connector and every document it imported (the bucket is not changed). `204` |
| `POST /connectors/{id}/sync` | Start a sync now. `202` with a new run, or `200` with the run already queued or running |
| `GET /connectors/{id}/sync-runs?limit=20&offset=0` | Sync history, newest first |
| `GET /connectors/{id}/sync-runs/{run_id}` | One run, with up to 50 failed or skipped objects |
| `POST /connectors/{id}/sync-runs/{run_id}/cancel` | Cancel a queued or running sync |

Errors use the usual envelope: `{"error": {"code": "…", "message": "…", "request_id": "…"}}`.

## Creating a connector

```http
POST /api/v1/connectors
Content-Type: application/json

{
  "name": "Team handbook",
  "provider": "s3",                 // optional; "s3" is the only provider
  "bucket": "team-docs",
  "prefix": "handbook/",            // optional; "" imports the whole bucket
  "sync_interval_minutes": 60,      // 5 to 10080, default 60
  "enabled": true                   // automatic syncs; manual syncs always work
}
```

The prefix is normalized to end in `/`. Unknown fields are rejected (`validation_error`),
so a request that tries to send credentials or an endpoint fails instead of being ignored.

| Status and code | When |
| --- | --- |
| `422 invalid_bucket` / `invalid_prefix` / `invalid_name` | Not a valid S3 bucket name; a prefix with `..`, `.`, empty segments, a leading `/`, backslashes or control characters; an empty name |
| `422 source_not_allowed` | The bucket and prefix are not covered by `S3_ALLOWED_SOURCES` |
| `409 connector_exists` | A connector already uses this bucket and prefix |
| `409 connector_overlap` | Another connector covers a parent or child of this prefix |
| `409 connector_name_taken` | The name is in use |
| `503 connectors_not_configured` | `S3_ALLOWED_SOURCES` is empty on the server |

### Connector

```json
{
  "id": "6c1e…",
  "name": "Team handbook",
  "provider": "s3",
  "bucket": "team-docs",
  "prefix": "handbook/",
  "uri": "s3://team-docs/handbook/",
  "enabled": true,
  "sync_interval_minutes": 60,
  "next_sync_at": "2026-10-17T08:24:00Z",
  "last_sync_started_at": "2026-10-17T07:24:00Z",
  "last_sync_finished_at": "2026-10-17T07:24:02Z",
  "last_success_at": "2026-10-17T07:24:02Z",
  "last_sync_status": "succeeded",
  "last_error_code": null,
  "last_error_message": null,
  "document_count": 3,
  "created_at": "…",
  "updated_at": "…"
}
```

`GET /connectors/{id}` adds `failed_object_count` (objects whose current version was
rejected), `deleted_object_count` (objects removed from the bucket, kept for audit),
`active_run` and `latest_run`.

## Updating

`PATCH` accepts `name`, `sync_interval_minutes` and `enabled`. Sending `bucket` or `prefix`
returns `422 immutable_field`: documents are tied to the objects they came from, so a
different source needs a new connector. Disabling clears `next_sync_at`. Enabling, or changing
the interval, schedules the next sync one interval after the last one finished (at once if
the connector never synced or that time has passed).

## Sync runs

```json
{
  "id": "b3d2…",
  "connector_id": "6c1e…",
  "trigger": "manual",              // or "scheduled"
  "status": "partial",              // pending, running, succeeded, partial, failed, cancelled
  "attempts": 1,
  "max_attempts": 3,
  "cancel_requested": false,
  "run_after": "…",
  "objects_scanned": 12,
  "documents_imported": 2,
  "documents_updated": 1,
  "documents_unchanged": 7,
  "documents_removed": 1,
  "objects_skipped": 1,
  "objects_failed": 1,
  "error_code": null,
  "error_message": null,
  "created_at": "…",
  "started_at": "…",
  "finished_at": "…"
}
```

The counts describe the latest attempt. `GET …/sync-runs/{run_id}` adds `failures`:

```json
[{"key": "handbook/scan.pdf", "code": "object_too_large", "message": "…", "kind": "failed"},
 {"key": "handbook/logo.png", "code": "unsupported_file_type", "message": "…", "kind": "skipped"}]
```

`POST …/cancel` returns the run. A queued run becomes `cancelled` at once; a running run
gets `cancel_requested: true` and stops before its next object. Cancelling a finished run
returns `409 sync_not_active`. Unknown connectors return `404 connector_not_found`, unknown
runs `404 sync_run_not_found`.

## Documents

Documents list and detail responses include `source_type` (`upload` or `s3`).
`GET /documents/{id}` includes `source` for imported documents:

```json
"source": {
  "connector_id": "6c1e…",
  "connector_name": "Team handbook",
  "provider": "s3",
  "bucket": "team-docs",
  "object_key": "handbook/経費精算.txt",
  "uri": "s3://team-docs/handbook/経費精算.txt",
  "etag": "9b2cf535f27731c974343645a3985328",
  "version_id": "3HL4kqtJlcpXroDTDmJ.rmSpXd3dIbrHY",
  "size_bytes": 1024,
  "last_modified": "2026-10-17T07:20:11Z",
  "last_synced_at": "2026-10-17T07:24:02Z"
}
```

`source` is `null` for uploads. The ETag is an opaque version token, not a checksum.
