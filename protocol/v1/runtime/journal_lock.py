"""Shared journal locks that also work on read-only bind mounts."""
from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


@contextmanager
def journal_lock(root: Path, lock_path: Path, *, exclusive: bool,
                 read_only: bool = False) -> Iterator[None]:
    if read_only:
        if exclusive:
            raise PermissionError("Read-only journal cannot be changed")
        # Use the writer's existing inode: a separate tmpfs lock would not
        # synchronize with writers. Never create a lock on a read-only store.
        flags = os.O_RDONLY | os.O_NOFOLLOW
    else:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        flags = os.O_RDWR | os.O_CREAT
    fd = os.open(lock_path, flags, 0o600)
    with os.fdopen(fd, "r" if read_only else "r+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
