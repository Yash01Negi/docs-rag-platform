#!/bin/sh
# Prepares the local MinIO emulator (docker compose --profile s3-local). Idempotent.
#
# Creates the sample bucket, uploads the sample documents under handbook/, and creates a
# read-only user whose keys are the worker's AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY from
# .env. The user's policy matches the least-privilege IAM policy in docs/s3-setup.md
# (s3:ListBucket on the bucket, s3:GetObject on its objects), so the worker can list and
# read this bucket only. Nothing here is used with Amazon S3.
set -eu

MINIO_URL="${MINIO_URL:-http://minio:9000}"
mc alias set local "$MINIO_URL" "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
mc mb --ignore-existing "local/$S3_LOCAL_BUCKET"
mc version enable "local/$S3_LOCAL_BUCKET" >/dev/null
if [ -d /setup/samples ]; then
  mc mirror --overwrite /setup/samples "local/$S3_LOCAL_BUCKET"
fi

if [ -n "$S3_LOCAL_READER_ACCESS_KEY" ] && [ -n "$S3_LOCAL_READER_SECRET_KEY" ]; then
  policy=/tmp/yashrag-reader-policy.json
  cat >"$policy" <<JSON
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": ["arn:aws:s3:::$S3_LOCAL_BUCKET"]},
    {"Effect": "Allow", "Action": ["s3:GetObject"], "Resource": ["arn:aws:s3:::$S3_LOCAL_BUCKET/*"]}
  ]
}
JSON
  mc admin policy create local yashrag-reader "$policy" >/dev/null
  mc admin user add local "$S3_LOCAL_READER_ACCESS_KEY" "$S3_LOCAL_READER_SECRET_KEY" >/dev/null
  # Attaching a policy that is already attached fails; that is fine on a re-run.
  mc admin policy attach local yashrag-reader --user "$S3_LOCAL_READER_ACCESS_KEY" >/dev/null 2>&1 \
    || mc admin user info local "$S3_LOCAL_READER_ACCESS_KEY" | grep -q yashrag-reader
  echo "read-only user ready for the worker (policy yashrag-reader)"
else
  echo "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY are empty in .env: no worker user created"
fi
echo "bucket $S3_LOCAL_BUCKET ready: $(mc ls --recursive "local/$S3_LOCAL_BUCKET" | wc -l) objects"
