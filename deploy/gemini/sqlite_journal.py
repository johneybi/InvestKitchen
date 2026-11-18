"""Offline journal transition; the operator must stop every database writer first.

Never restore an older authentication snapshot during ordinary rollback: doing so
could revive revoked grants. Rollback changes the journal on the current database.
"""
import hashlib
import os
from pathlib import Path
import sqlite3
import stat
from contextlib import closing


def full_wal_storage_path(database):
    """Authelia 4.39.28 appends '?_txlock=immediate' to Local.Path.

    Explicit driver options retain FULL durability and immediate transactions.
    The final ignored parameter contains the issuer's appended suffix. This is
    version-specific and requires real-issuer verification before any upgrade.
    The canonical config path stays plain; use a pinned service environment override.
    """
    path = Path(database)
    if not path.is_absolute() or any(c in str(path) for c in ('?', '#', '&', '%')):
        raise ValueError('unsupported database path')
    return str(path)+'?_journal_mode=WAL&_synchronous=FULL&_txlock=immediate&_authelia_tail='


def logical_digest(connection):
    digest = hashlib.sha256()
    for statement in connection.iterdump():
        digest.update(statement.encode('utf8'))
        digest.update(b'\n')
    return digest.hexdigest()


def checked_path(path):
    path = Path(path)
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('database path must be absolute and contain no symlinks')
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError('database must be a private regular file')
    return path


def change_journal(database, target, backup=None):
    if target not in ('wal', 'delete'):
        raise ValueError('unsupported journal mode')
    path = checked_path(database)
    with closing(sqlite3.connect(path.as_uri()+'?mode=rw', uri=True, timeout=2)) as conn:
        if conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise ValueError('database integrity check failed')
        before = logical_digest(conn)
        original = conn.execute('PRAGMA journal_mode').fetchone()[0]
        if backup is not None:
            destination = Path(backup)
            parent = destination.parent
            if (not parent.is_absolute() or any(p.is_symlink() for p in (parent, *parent.parents))
                    or stat.S_IMODE(parent.stat().st_mode) != 0o700):
                raise ValueError('backup directory must be private')
            fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            with closing(sqlite3.connect(str(destination))) as saved:
                conn.backup(saved)
                if logical_digest(saved) != before:
                    raise ValueError('backup verification failed')
            with destination.open('rb') as saved:
                os.fsync(saved.fileno())
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        # FULL durability is never relaxed by this utility. The issuer driver
        # must independently retain FULL for each of its own connections.
        conn.execute('PRAGMA synchronous=FULL')
        if original == 'wal':
            if conn.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()[0] != 0:
                raise ValueError('database is busy; stop all writers')
        actual = conn.execute('PRAGMA journal_mode='+target).fetchone()[0]
        if actual != target or logical_digest(conn) != before:
            raise ValueError('journal transition verification failed')
    # Check the persisted mode using a separate connection.
    with closing(sqlite3.connect(path.as_uri()+'?mode=rw', uri=True, timeout=2)) as conn:
        if (conn.execute('PRAGMA journal_mode').fetchone()[0] != target
                or conn.execute('PRAGMA integrity_check').fetchall() != [('ok',)]
                or logical_digest(conn) != before):
            raise ValueError('persisted journal verification failed')
    return {'previous_mode': original, 'journal_mode': target, 'logical_data_preserved': True}
