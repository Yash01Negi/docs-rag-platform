"""Storage for original uploaded files.

Files are addressed by opaque, server-generated keys (32 lowercase hex characters). User
input never becomes part of a filesystem path, and keys are validated before use, so a key
cannot escape the storage root. :class:`DocumentStorage` is the seam for a future
Amazon S3 implementation; the rest of the application only uses this interface.
"""

import contextlib
import os
import secrets
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import BinaryIO, Protocol

_KEY_LENGTH = 32
_HEX = frozenset("0123456789abcdef")


class InvalidStorageKeyError(ValueError):
    pass


class StoredFileNotFoundError(FileNotFoundError):
    pass


def new_storage_key() -> str:
    return secrets.token_hex(_KEY_LENGTH // 2)


def validate_storage_key(key: str) -> str:
    if len(key) != _KEY_LENGTH or not set(key) <= _HEX:
        raise InvalidStorageKeyError("invalid storage key")
    return key


class DocumentStorage(Protocol):
    def put_file(self, key: str, source: Path) -> None:
        """Store ``source`` under ``key``. ``source`` may be moved or deleted afterwards."""

    def open(self, key: str) -> BinaryIO:
        """Open the stored file for binary reading. Raises StoredFileNotFoundError."""

    def local_path(self, key: str) -> Path:
        """A local filesystem path for processing (a temporary download for remote stores)."""

    def delete(self, key: str) -> bool:
        """Delete the file. Returns False if it did not exist."""

    def exists(self, key: str) -> bool: ...

    def iter_keys_older_than(self, seconds: float) -> Iterator[str]:
        """Keys of stored files last modified more than ``seconds`` ago."""


class LocalFileStorage:
    """Stores files on the local filesystem as ``<root>/<key[:2]>/<key>``."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def _path(self, key: str) -> Path:
        validate_storage_key(key)
        path = (self.root / key[:2] / key).resolve()
        # Defence in depth: validation already rules out separators and "..".
        if not path.is_relative_to(self.root):
            raise InvalidStorageKeyError("invalid storage key")
        return path

    def put_file(self, key: str, source: Path) -> None:
        target = self._path(key)
        target.parent.mkdir(parents=True, exist_ok=True)
        partial = target.with_name(f".{key}.partial")
        # Copy to a temporary name and rename, so a crash never leaves a truncated file
        # under a valid key.
        shutil.copyfile(source, partial)
        os.chmod(partial, 0o640)
        os.replace(partial, target)

    def open(self, key: str) -> BinaryIO:
        try:
            return self._path(key).open("rb")
        except FileNotFoundError as exc:
            raise StoredFileNotFoundError(key) from exc

    def local_path(self, key: str) -> Path:
        path = self._path(key)
        if not path.is_file():
            raise StoredFileNotFoundError(key)
        return path

    def delete(self, key: str) -> bool:
        try:
            self._path(key).unlink()
        except FileNotFoundError:
            return False
        with contextlib.suppress(OSError):
            self._path(key).parent.rmdir()  # only succeeds when the shard is empty
        return True

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def iter_keys_older_than(self, seconds: float) -> Iterator[str]:
        if not self.root.is_dir():
            return
        cutoff = time.time() - seconds
        for shard in self.root.iterdir():
            if not shard.is_dir():
                continue
            for entry in shard.iterdir():
                try:
                    key = validate_storage_key(entry.name)
                except InvalidStorageKeyError:
                    continue
                if entry.is_file() and entry.stat().st_mtime < cutoff:
                    yield key
