import os
import time
from pathlib import Path

import pytest

from app.storage import (
    InvalidStorageKeyError,
    LocalFileStorage,
    StoredFileNotFoundError,
    new_storage_key,
    validate_storage_key,
)


@pytest.fixture
def storage(tmp_path: Path) -> LocalFileStorage:
    return LocalFileStorage(tmp_path / "store")


def _source(tmp_path: Path, data: bytes = b"hello") -> Path:
    path = tmp_path / "source.bin"
    path.write_bytes(data)
    return path


def test_generated_keys_are_valid_and_unique() -> None:
    keys = {new_storage_key() for _ in range(100)}
    assert len(keys) == 100
    assert all(validate_storage_key(key) == key for key in keys)


@pytest.mark.parametrize(
    "key",
    [
        "../../etc/passwd",
        "/etc/passwd",
        "..",
        "",
        "A" * 32,  # upper case is not produced by new_storage_key
        "a" * 31,
        "a" * 33,
        "0123456789abcdef0123456789abcde/",
        "0123456789abcdef0123456789abc\\de",
    ],
)
def test_invalid_keys_are_rejected(storage: LocalFileStorage, key: str) -> None:
    with pytest.raises(InvalidStorageKeyError):
        storage.open(key)


def test_put_open_delete_round_trip(storage: LocalFileStorage, tmp_path: Path) -> None:
    key = new_storage_key()
    storage.put_file(key, _source(tmp_path, "日本語".encode()))

    assert storage.exists(key)
    with storage.open(key) as handle:
        assert handle.read().decode() == "日本語"
    assert storage.local_path(key).is_relative_to(storage.root)
    assert storage.delete(key) is True
    assert storage.delete(key) is False
    assert not storage.exists(key)
    with pytest.raises(StoredFileNotFoundError):
        storage.open(key)
    with pytest.raises(StoredFileNotFoundError):
        storage.local_path(key)


def test_put_does_not_leave_partial_files(storage: LocalFileStorage, tmp_path: Path) -> None:
    key = new_storage_key()
    storage.put_file(key, _source(tmp_path))
    files = [p.name for p in storage.root.rglob("*") if p.is_file()]
    assert files == [key]


def test_iter_keys_older_than(storage: LocalFileStorage, tmp_path: Path) -> None:
    old, new = new_storage_key(), new_storage_key()
    storage.put_file(old, _source(tmp_path))
    storage.put_file(new, _source(tmp_path))
    past = time.time() - 7200
    os.utime(storage.local_path(old), (past, past))

    assert list(storage.iter_keys_older_than(3600)) == [old]


def test_iter_keys_on_missing_root(tmp_path: Path) -> None:
    assert list(LocalFileStorage(tmp_path / "missing").iter_keys_older_than(0)) == []
