#!/bin/sh
# Prepares the local MinIO emulator (docker compose --profile s3-local). Idempotent.
#
# Creates the sample bucket, uploads the sample documents under handbook/, and creates a
# read-only user whose keys are the worker's AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY from
# .env, so the worker gets list and read access only. Nothing here is used with Amazon S3.
set -eu

mc alias set local http://minio:9000 "$MINIO_ROOT_USER" "$MINIO_ROOT_PASSWORD" >/dev/null
mc mb --ignore-existing "local/$S3_LOCAL_BUCKET"
mc version enable "local/$S3_LOCAL_BUCKET" >/dev/null
mc mirror --overwrite /setup/samples "local/$S3_LOCAL_BUCKET"

if [ -n "$S3_LOCAL_READER_ACCESS_KEY" ] && [ -n "$S3_LOCAL_READER_SECRET_KEY" ]; then
  mc admin user add local "$S3_LOCAL_READER_ACCESS_KEY" "$S3_LOCAL_READER_SECRET_KEY" >/dev/null
  mc admin policy attach local readonly --user "$S3_LOCAL_READER_ACCESS_KEY" >/dev/null 2>&1 || true
  echo "read-only user ready for the worker"
else
  echo "AWS_ACCESS_KEY_ID / AWS_SECRET_ACCESS_KEY are empty in .env: no worker user created"
fi
echo "bucket $S3_LOCAL_BUCKET ready: $(mc ls --recursive "local/$S3_LOCAL_BUCKET" | wc -l) objects"
