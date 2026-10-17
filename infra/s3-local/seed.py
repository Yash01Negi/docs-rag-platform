"""Fills the local S3 emulator with the sample documents. Idempotent.

Creates the bucket (with versioning, like a typical document bucket) and uploads every file
under samples/ with its relative path as the key, so samples/handbook/security.md becomes
s3://<bucket>/handbook/security.md. Used by the s3-local Compose profile and by CI; never
pointed at Amazon S3 (it refuses to run without S3_ENDPOINT_URL).

    S3_ENDPOINT_URL=http://127.0.0.1:9000 S3_LOCAL_BUCKET=yashrag-local python seed.py
"""

import os
import sys
from pathlib import Path

import boto3

SAMPLES = Path(__file__).resolve().parent / "samples"


def main() -> int:
    endpoint = os.environ.get("S3_ENDPOINT_URL", "")
    bucket = os.environ.get("S3_LOCAL_BUCKET", "yashrag-local")
    if not endpoint:
        print("S3_ENDPOINT_URL is not set; this script only seeds a local emulator")
        return 1
    # The emulator accepts any keys; these are only used when none are configured.
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name=os.environ.get("S3_REGION") or "us-east-1",
        aws_access_key_id=os.environ.get("S3_SEED_ACCESS_KEY_ID", "local-seed"),
        aws_secret_access_key=os.environ.get("S3_SEED_SECRET_ACCESS_KEY", "local-seed"),
    )
    existing = {item["Name"] for item in client.list_buckets().get("Buckets", [])}
    if bucket not in existing:
        client.create_bucket(Bucket=bucket)
    client.put_bucket_versioning(Bucket=bucket, VersioningConfiguration={"Status": "Enabled"})
    count = 0
    for path in sorted(SAMPLES.rglob("*")):
        if path.is_file():
            key = path.relative_to(SAMPLES).as_posix()
            client.put_object(Bucket=bucket, Key=key, Body=path.read_bytes())
            count += 1
    print(f"bucket {bucket} ready: {count} sample objects")
    return 0


if __name__ == "__main__":
    sys.exit(main())
