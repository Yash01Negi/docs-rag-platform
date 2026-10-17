# Setting up S3 connectors

This guide sets up the backend so the Connectors page can import from Amazon S3, or from a
local MinIO emulator for trying it without AWS. How syncing behaves:
[connectors.md](connectors.md).

> **Local development only.** YashRAG has no authentication: anyone who can reach it can
> read every imported document. Keep it on your machine or a trusted network, and give the
> worker read-only access to the buckets it needs and nothing else.

Credentials are configured **only on the worker**, through the standard AWS credential
chain. They are never typed into the browser, never stored in PostgreSQL and never returned
by the API. The API container gets no AWS credentials at all.

## 1. Create a least-privilege IAM policy

The connector makes exactly two S3 calls: `ListObjectsV2` (permission `s3:ListBucket`) and
`GetObject` (permission `s3:GetObject`). It never writes, deletes or changes anything in S3.
Replace `team-docs` and `handbook/` with your bucket and prefix:

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "ListTheConnectorPrefix",
      "Effect": "Allow",
      "Action": "s3:ListBucket",
      "Resource": "arn:aws:s3:::team-docs",
      "Condition": {
        "StringLike": { "s3:prefix": ["handbook/", "handbook/*"] }
      }
    },
    {
      "Sid": "ReadObjectsUnderThePrefix",
      "Effect": "Allow",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::team-docs/handbook/*"
    }
  ]
}
```

- For a connector on a whole bucket, drop the `Condition` and use `arn:aws:s3:::team-docs/*`.
- `s3:GetObjectVersion` is not needed: the connector reads the current version and records
  the version ID that `GetObject` returns.
- If the bucket uses SSE-KMS with a customer-managed key, also allow `kms:Decrypt` on that
  key.
- Attach the policy to a dedicated IAM user or role for YashRAG, not to your own
  administrator identity.

## 2. Give the worker credentials

Pick one. All of them are read by boto3's default credential chain in the worker.

### Option A: an AWS profile (recommended for a laptop)

Configure a profile on your machine (`aws configure --profile yashrag`, or an IAM Identity
Center profile with `aws configure sso` and `aws sso login --profile yashrag`). Then:

- **Running the worker on the host** (README, Option B): set `AWS_PROFILE=yashrag` in `.env`.
- **Running the worker in Docker:** mount your AWS configuration into the worker, read-only,
  with a `docker-compose.override.yml` (not committed):

  ```yaml
  services:
    worker:
      volumes:
        - ${HOME}/.aws:/home/appuser/.aws:ro
  ```

  and set `AWS_PROFILE=yashrag` in `.env`. The container runs as UID 10001, so on Linux the
  files must be readable by that user. IAM Identity Center sessions expire; run
  `aws sso login` again on the host when syncs start failing with `credentials_missing` or
  `access_denied`. (Profile mounting has not been tested in Docker during development.)

### Option B: access keys in `.env`

```bash
AWS_ACCESS_KEY_ID=AKIA…
AWS_SECRET_ACCESS_KEY=…
# AWS_SESSION_TOKEN=…   # for temporary credentials
```

`.env` is ignored by git; never commit real keys. Prefer short-lived credentials, and rotate
long-lived keys. Compose passes these to the worker only.

### Option C: an IAM role

When the worker runs on AWS compute (EC2 instance profile, ECS task role), leave every
`AWS_*` variable empty and boto3 uses the role. YashRAG has no deployment setup for this
yet; it is mentioned only because the credential chain supports it.

## 3. Allow the source and set the region

In `.env`:

```bash
S3_ALLOWED_SOURCES=team-docs/handbook/   # or just team-docs for the whole bucket
S3_REGION=ap-northeast-1                 # the bucket's region
```

`S3_ALLOWED_SOURCES` is a comma-separated list of buckets or `bucket/prefix/` scopes. The
form only accepts connectors inside these, and the worker re-checks them on every run. A
connector for a prefix outside the list is refused, so a typo cannot import from the wrong
bucket.

Restart the API and the worker (`docker compose up -d --force-recreate backend worker`),
then open <http://localhost:5173/connectors>, add a connector for the same bucket and
prefix, and watch the first sync.

## Trying it without AWS: MinIO

The optional `s3-local` Compose profile starts [MinIO](https://min.io/) on `127.0.0.1`
and fills a versioned bucket with three sample documents (English and Japanese) under
`handbook/`. A one-off job creates a read-only MinIO user for the worker with the same two
permissions as the IAM policy above.

```bash
# .env (local-only values you choose; MINIO_ROOT_PASSWORD needs 8+ characters)
MINIO_ROOT_PASSWORD=choose-a-local-password
AWS_ACCESS_KEY_ID=yashrag-reader
AWS_SECRET_ACCESS_KEY=choose-a-reader-secret
S3_ENDPOINT_URL=http://minio:9000
S3_REGION=us-east-1
S3_ALLOWED_SOURCES=yashrag-local
```

```bash
docker compose --profile s3-local up --build
```

Then add a connector for bucket `yashrag-local`, prefix `handbook/`. The MinIO console is at
<http://127.0.0.1:9001> (log in with `MINIO_ROOT_USER` / `MINIO_ROOT_PASSWORD`); upload,
replace or delete files there and press **Sync now** to see changes picked up.

Notes:

- `S3_ENDPOINT_URL` is a server-side setting only. It must be `http(s)://host[:port]`
  without credentials or a path; the browser can never choose an endpoint. Leave it empty
  for Amazon S3.
- When you switch back to Amazon S3, clear `S3_ENDPOINT_URL` and replace the MinIO keys.
- The MinIO images are pinned community releases from MinIO's registry on `quay.io` (the
  Docker Hub repository is no longer available, and MinIO has stopped publishing new
  community images). Treat the profile as a testing aid only. CI runs the smoke tests
  against the same images.
- Running the API and worker on the host instead of in Docker? Use
  `S3_ENDPOINT_URL=http://127.0.0.1:9000`.

## Troubleshooting

| Error code on the run | Likely cause |
| --- | --- |
| `credentials_missing` | No credentials in the worker's environment, or `AWS_PROFILE` names a profile that does not exist in the container |
| `access_denied` | The policy does not allow `s3:ListBucket` on the bucket (with this prefix) or `s3:GetObject` on its objects |
| `bucket_not_found` | Wrong bucket name, or the bucket is in another account |
| `wrong_region` | `S3_REGION` does not match the bucket's region |
| `s3_unreachable` | No network route to S3 (proxy, DNS, or MinIO not running) |
| `source_not_allowed` | The connector's bucket or prefix was removed from `S3_ALLOWED_SOURCES` |
| `too_many_objects` | More than `S3_MAX_OBJECTS_PER_SYNC` objects under the prefix; narrow the prefix or raise the limit |

Error messages never include credentials, signatures or request IDs. The worker logs
(`docker compose logs worker`) show the same codes with connector and run IDs.
