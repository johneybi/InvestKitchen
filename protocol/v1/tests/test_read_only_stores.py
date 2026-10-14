from __future__ import annotations

import fcntl
import os

import pytest

from protocol.v1.runtime.journal_lock import journal_lock
from protocol.v1.runtime.native_knowledge_store import NativeKnowledgeStore
from protocol.v1.runtime.native_write_store import NativeWriteStore
from protocol.v1.runtime.portfolio_checkpoint import PortfolioCheckpointStore
from protocol.v1.runtime.reference_composition import build_reference_gateway


@pytest.mark.parametrize("factory,reader", [
    (NativeWriteStore, "read_journal"),
    (NativeKnowledgeStore, "read_journal"),
    (PortfolioCheckpointStore, "read_all"),
])
def test_read_only_store_requires_existing_writer_lock_without_initializing(tmp_path, factory, reader):
    root = tmp_path / "not-initialized"
    store = factory(root, read_only=True)
    with pytest.raises(FileNotFoundError):
        getattr(store, reader)()
    assert not root.exists()
    root.mkdir()
    store.lock_path.touch(mode=0o400)
    inode = store.lock_path.stat().st_ino
    assert getattr(store, reader)() == []
    assert store.lock_path.stat().st_ino == inode
    assert list(root.iterdir()) == [store.lock_path]
    store.lock_path.unlink()
    store.lock_path.symlink_to(tmp_path / "another-lock")
    with pytest.raises(OSError):
        getattr(store, reader)()


def test_shared_reader_lock_blocks_existing_writer_inode(tmp_path):
    lock = tmp_path / ".write-journal.lock"
    lock.touch(mode=0o600)
    with journal_lock(tmp_path, lock, exclusive=False, read_only=True):
        writer_fd = os.open(lock, os.O_RDWR)
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(writer_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            os.close(writer_fd)
    with journal_lock(tmp_path, lock, exclusive=True):
        pass


def test_read_only_stores_refuse_all_commit_entry_points(tmp_path):
    with pytest.raises(PermissionError):
        NativeWriteStore(tmp_path, read_only=True).commit_operation(
            preview={}, resource={}, principal={}, validation=None, committed_at=None)
    with pytest.raises(PermissionError):
        NativeKnowledgeStore(tmp_path, read_only=True).commit(
            preview={}, validation=None, committed_at=None)
    with pytest.raises(PermissionError):
        PortfolioCheckpointStore(tmp_path, read_only=True).append({})
    assert not list(tmp_path.iterdir())


def test_read_only_composition_rejects_write_authority_before_reading_paths(tmp_path):
    with pytest.raises(ValueError, match="Read-only stores"):
        build_reference_gateway(runtime_root=tmp_path, manifest=tmp_path / "missing.json",
                                read_only_stores=True, write_principal={})
