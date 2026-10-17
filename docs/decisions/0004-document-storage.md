# ADR 0004: Store original documents behind a storage interface, keyed by generated IDs

- **Status:** Accepted
- **Date:** 2026-10-12

## Context

Uploaded originals must be kept: the worker reads them after the upload request has
returned, reindexing needs them again later, and users can download them. Requirements:

- user-supplied filenames must never become filesystem paths (path traversal, collisions,
  Unicode and case quirks);
- the same content uploaded twice should not be stored or indexed twice;
- local development and Docker Compose need something that works with no cloud account,
  while a later cloud deployment will want object storage such as S3;
- the database and the stored file must not drift apart in ways that lose data or leave
  documents pointing at nothing.

Options considered:

1. **Files in PostgreSQL** (`bytea` or large objects). Transactional with the metadata, but
   20 MB blobs bloat the database, backups and replication.
2. **Files on local disk, paths built from the original filename.** Simple but unsafe, and
   ties the design to one machine.
3. **A small storage interface, local filesystem implementation now, object storage later.**

## Decision

Use option 3.

- `DocumentStorage` (`backend/app/storage.py`) is a protocol with `put`, `open`,
  `local_path`, `delete`, `exists` and `iter_keys_older_than`. `LocalFileStorage` is the
  only implementation. An S3 implementation would map keys to object names; the worker
  already copes with storage that cannot hand out a local path because the interface
  separates `open` from `local_path`.
- **Keys are generated**, 32 lowercase hex characters (a UUID4 hex). The key is validated on
  every call, so a key containing `/`, `..` or anything else is rejected before it can reach
  the filesystem. Files live at `<STORAGE_DIR>/<key[:2]>/<key>`.
- **Writes are atomic**: the file is written to a `.partial` sibling and renamed into place.
- The original filename is kept only as metadata (`documents.filename`), after
  sanitizing: basename only, Unicode NFC, control and path characters removed, at most 255
  characters. Downloads send it in an RFC 5987 `Content-Disposition` header.
- In Docker Compose the storage root is the `uploads` named volume, mounted into the API and
  the worker.

### Deduplication

> **Amended by [ADR 0011](0011-s3-sync-and-deletion.md):** since Phase 5 the unique index
> covers uploads only (`source_type = 'upload'`); documents imported from S3 are tracked by
> their object instead.

Documents are deduplicated by the **SHA-256 of their content**, enforced by a unique index
on `documents.sha256`. The API computes the hash while streaming the upload to a staging
file. If a document with the same hash exists, the upload returns `200` with
`duplicate: true` and the existing document; nothing new is stored or queued. Two identical
uploads racing each other both pass the lookup; the second insert hits the unique index, its
transaction rolls back, its stored file is deleted, and it returns the winner as a
duplicate. The filename of the first upload is kept.

### Deletion and orphan cleanup

`DELETE /api/v1/documents/{id}` deletes the database rows first (document, chunks and jobs,
by cascade) and the stored file second. If the file deletion fails, the API still returns
`204`: the document is gone from every user-visible place and the file is now an
orphan. Deleting the file first would risk the opposite, a document that exists but whose
file is gone, which is worse.

Orphans can come from that case, or from a crash between storing a file and committing its
row. The worker sweeps them when it starts: it lists stored keys older than one hour and
deletes those that no document references. The one-hour age keeps the sweep away from
uploads that are still in flight. There is no periodic sweep yet, so orphans from a
long-running worker are only removed at its next restart.

## Consequences

- Filenames cannot influence where anything is written.
- Moving to S3 means implementing the protocol and choosing it in configuration; the
  service code does not change. That implementation, its credentials and its consistency
  behavior are future work.
- Storage and database are not one transaction. The order of operations and the orphan
  sweep keep them consistent in the direction that matters (no document without its file),
  at the cost of temporary orphan files.
- Deduplication is global. That is correct for a single-user tool. With authentication
  and multiple users or tenants (not implemented), a global hash index would reveal whether
  someone else uploaded the same file; deduplication would need to become per-owner.
- Two users uploading the same content under different names see one document with the
  first name.
- There are no backups of the `uploads` volume; it is local development storage.
