# S3 smoke tests

The normal test suite exercises connectors against **moto**, an in-process S3 emulator, with
the real boto3 client, the real worker and a real PostgreSQL database. This page covers the
opt-in smoke test that goes over HTTP to a running S3 API: a local emulator such as MinIO,
or a real Amazon S3 bucket.

| What | Talks to | Proves | Runs |
| --- | --- | --- | --- |
| `tests/test_connector_*.py` | moto (in process) | Sync semantics, queue, API, safety rules | Every `pytest` run and CI |
| `tests/test_s3_smoke.py` with MinIO | MinIO over HTTP | The same code over a real network connection to an independent S3 implementation, with a read-only user and pagination | CI job "S3 smoke (MinIO emulator)"; locally on request |
| `tests/test_s3_smoke.py` with Amazon S3 | AWS | Credentials, IAM policy, region and real S3 behaviour (ETags, versioning) | Only when you run it yourself |

**Passing against an emulator is not the same as working against AWS.** Emulators accept
requests that AWS might refuse (IAM conditions, regions, signature details) and produce
different ETags and version IDs. As of Phase 5, the connector has **not** been run against
real Amazon S3.

The test is skipped unless `RUN_S3_SMOKE_TESTS=1`. It needs a migrated test database
(`TEST_DATABASE_URL`, as for the integration tests) but no embedding model: it uses the fake
embedding provider.

## Against MinIO

Start MinIO (the Compose profile from [s3-setup.md](s3-setup.md), or any MinIO), then from
`backend/`:

```bash
RUN_S3_SMOKE_TESTS=1 \
S3_SMOKE_BUCKET=yashrag-local S3_SMOKE_WRITE=1 \
S3_ENDPOINT_URL=http://127.0.0.1:9000 S3_REGION=us-east-1 \
AWS_ACCESS_KEY_ID=yashrag-reader AWS_SECRET_ACCESS_KEY=… \
S3_SMOKE_WRITER_ACCESS_KEY_ID=yashrag-admin S3_SMOKE_WRITER_SECRET_ACCESS_KEY=… \
TEST_DATABASE_URL=postgresql+psycopg://… \
uv run pytest tests/test_s3_smoke.py -m s3_smoke -v
```

With `S3_SMOKE_WRITE=1` the test creates objects under a random prefix
(`yashrag-smoke/<random>/`), then checks, through the real API and worker:

1. The first sync imports an English Markdown file and a Japanese text file and skips a
   PNG; both are searchable, and their source shows `s3://…` and an ETag.
2. A second sync downloads nothing (all unchanged).
3. After the Markdown file is replaced and the text file deleted, a sync updates one
   document and removes one; the old text is no longer found.
4. Deleting the connector removes its documents.

It deletes the objects it created when it finishes. The connector runs with
`AWS_ACCESS_KEY_ID` (the read-only user); the test's own writes use the
`S3_SMOKE_WRITER_*` keys (any user that can write; without them it uses the same
credentials as the connector).

Read-only mode (no `S3_SMOKE_WRITE`): set `S3_SMOKE_PREFIX` to a prefix that already holds
supported documents, for example `handbook/` in the sample bucket. The test imports them,
checks that a second sync is a no-op, and changes nothing in the bucket.

## Against Amazon S3

Use read-only mode unless you have a scratch bucket you are happy for the test to write to.

1. Create or choose a bucket and upload two or three small PDF, Markdown or text files under
   a prefix such as `yashrag-smoke-readonly/`.
2. Give an IAM identity the policy from [s3-setup.md](s3-setup.md) for that prefix, and make
   its credentials available (for example `AWS_PROFILE`).
3. From `backend/`:

   ```bash
   RUN_S3_SMOKE_TESTS=1 \
   S3_SMOKE_BUCKET=your-bucket S3_SMOKE_PREFIX=yashrag-smoke-readonly/ \
   S3_REGION=ap-northeast-1 AWS_PROFILE=yashrag \
   TEST_DATABASE_URL=postgresql+psycopg://… \
   uv run pytest tests/test_s3_smoke.py -m s3_smoke -v
   ```

Leave `S3_ENDPOINT_URL` unset. Costs are a few `LIST` and `GET` requests.

To also check updates and deletions, point `S3_SMOKE_BUCKET` at a **scratch** bucket and add
`S3_SMOKE_WRITE=1` with writer keys (or a profile that can `PutObject` and `DeleteObject`
under `yashrag-smoke/`).

## Manual end-to-end check in the UI

With the stack running (MinIO profile or real S3):

1. Open **Connectors**, add a connector for the allowed bucket and prefix, and wait for the
   first sync to show **Synced** with the expected counts.
2. Open **Documents**: the imported files carry an **S3** tag; a document's page shows its
   S3 location, ETag and version.
3. Ask a question on **Chat** that one of the files answers; the citation opens the
   imported document.
4. Replace one object and delete another in the bucket, press **Sync now**, and check that
   the counts show one updated and one removed, and that search reflects the change.
5. Stop the worker while a sync runs (`docker compose stop worker`), start it again, and
   check that the sync completes without duplicate documents.
6. Delete the connector and check that its documents are gone and the bucket is untouched.

Check the worker logs while you do this: they should show IDs, keys and counts, never file
contents or credentials.
