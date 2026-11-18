import os
import sqlite3

import pytest

from deploy.gemini.sqlite_journal import change_journal, full_wal_storage_path
from urllib.parse import parse_qs


def test_pinned_issuer_suffix_preserves_full_and_immediate_options():
    dsn=full_wal_storage_path('/synthetic/auth.sqlite3')+'?_txlock=immediate'
    filename,query=dsn.split('?',1)
    assert filename=='/synthetic/auth.sqlite3'
    options=parse_qs(query)
    assert options['_synchronous']==['FULL']
    assert options['_journal_mode']==['WAL']
    assert options['_txlock']==['immediate']
    with pytest.raises(ValueError):full_wal_storage_path('/synthetic/auth?bad')


@pytest.fixture
def database(tmp_path):
    path = tmp_path/'auth.sqlite3'
    with sqlite3.connect(path) as conn:
        conn.execute('create table grants(id integer primary key, revoked integer, data blob)')
        conn.execute('insert into grants values (1,0,?)', (b'synthetic-only',))
    conn.close()
    path.chmod(0o600)
    return path


def test_wal_backup_and_rollback_preserve_new_revocation(database, tmp_path):
    folder = tmp_path/'backup';folder.mkdir(mode=0o700)
    result = change_journal(database, 'wal', folder/'auth.sqlite3')
    assert result == dict(previous_mode='delete', journal_mode='wal', logical_data_preserved=True)
    with sqlite3.connect(database) as conn:
        conn.execute('update grants set revoked=1 where id=1')
        conn.execute('insert into grants values (2,0,?)', (b'new-synthetic-grant',))
    conn.close()  # The operator stops all issuer connections before rollback.
    change_journal(database, 'delete')
    with sqlite3.connect(database) as conn:
        assert conn.execute('select id,revoked from grants order by id').fetchall()==[(1,1),(2,0)]
    assert os.stat(folder/'auth.sqlite3').st_mode & 0o777 == 0o600


def test_backup_is_not_overwritten(database, tmp_path):
    folder=tmp_path/'backup';folder.mkdir(mode=0o700)
    saved=folder/'auth.sqlite3';saved.write_bytes(b'preserve')
    with pytest.raises(FileExistsError):change_journal(database,'wal',saved)
    assert saved.read_bytes()==b'preserve'


def test_busy_database_keeps_original_data(database):
    writer=sqlite3.connect(database);writer.execute('BEGIN EXCLUSIVE')
    try:
        with pytest.raises(sqlite3.OperationalError):change_journal(database,'wal')
    finally:writer.rollback();writer.close()
    with sqlite3.connect(database) as conn:
        assert conn.execute('PRAGMA journal_mode').fetchone()[0]=='delete'


def test_symlink_and_public_database_rejected(database,tmp_path):
    link=tmp_path/'link';link.symlink_to(database)
    with pytest.raises(ValueError):change_journal(link,'wal')
    database.chmod(0o644)
    with pytest.raises(ValueError):change_journal(database,'wal')


def test_unsafe_backup_directory_rejected(database,tmp_path):
    folder=tmp_path/'public';folder.mkdir(mode=0o755)
    with pytest.raises(ValueError):change_journal(database,'wal',folder/'copy')


def test_missing_database_is_not_created(tmp_path):
    with pytest.raises(FileNotFoundError):change_journal(tmp_path/'missing','wal')
    assert not (tmp_path/'missing').exists()
