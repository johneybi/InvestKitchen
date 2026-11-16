import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

SOURCE = Path(__file__).resolve().parents[3] / 'deploy/gemini/auth_diagnostics.py'
spec = importlib.util.spec_from_file_location('issuer_diagnostics', SOURCE)
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)


def test_only_categories_survive():
    event = diag.classify(json.dumps({'level': 'error', 'path': '/api/oidc/token?code=SECRET',
        'msg': 'Failed token request', 'error': 'invalid_client client_secret_basic not allowed SECRET',
        'username': 'PRIVATE', 'remote_ip': 'PRIVATE', 'body': 'password=SECRET'}))
    assert event == {'event': 'issuer_warning', 'level': 'error', 'route': '/api/oidc/token',
                     'markers': ['invalid_client', 'client_secret_basic', 'not allowed']}
    assert 'SECRET' not in json.dumps(event)
    assert 'PRIVATE' not in json.dumps(event)


@pytest.mark.parametrize('raw', [b'not json SECRET', b'[]', b'{"level":"info","msg":"SECRET"}',
                                 b'{"level":"error SECRET"}'])
def test_ignores_unstructured_or_unrecognized_levels(raw):
    assert diag.classify(raw) is None


def test_omits_unknown_path(tmp_path):
    event = diag.classify('{"level":"warn","path":"/private/SECRET"}')
    assert event['route'] == 'other'
    dest = tmp_path / 'events'
    diag.record(dest, event)
    assert dest.stat().st_mode & 0o777 == 0o600
    assert 'SECRET' not in dest.read_text()


def test_bounded_output_and_symlink_rejection(tmp_path):
    dest = tmp_path / 'events'
    dest.write_bytes(b'x' * diag.MAX_BYTES)
    dest.chmod(0o600)
    diag.record(dest, {'event': 'issuer_started'})
    assert dest.stat().st_size < 1024
    link = tmp_path / 'link'
    link.symlink_to(dest)
    with pytest.raises(OSError):
        diag.record(link, {'event': 'issuer_started'})


def test_public_file_rejected(tmp_path):
    dest = tmp_path / 'events'
    dest.touch(mode=0o644)
    dest.chmod(0o644)
    with pytest.raises(ValueError):
        diag.record(dest, {'event': 'issuer_started'})


def test_child_output_private_and_exit_preserved(tmp_path):
    dest = tmp_path / 'events'
    code = 'import sys; print(\'{"level":"error","msg":"invalid_grant SECRET"}\'); sys.exit(7)'
    result = subprocess.run([sys.executable, str(SOURCE), '--output', str(dest), '--',
                             sys.executable, '-c', code], capture_output=True)
    assert result.returncode == 7
    assert not result.stdout and not result.stderr
    assert 'SECRET' not in dest.read_text()
    assert 'invalid_grant' in dest.read_text()


def test_sigterm_forwarded(tmp_path):
    dest = tmp_path / 'events'
    child = subprocess.Popen([sys.executable, str(SOURCE), '--output', str(dest), '--',
                              sys.executable, '-c', 'import time; time.sleep(30)'])
    try:
        deadline = time.monotonic() + 5
        while not dest.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        assert dest.exists()
        child.send_signal(signal.SIGTERM)
        assert child.wait(timeout=3) == 143
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
