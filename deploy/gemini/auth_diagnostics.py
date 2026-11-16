"""Run an issuer unchanged while retaining only allowlisted diagnostic categories.

Raw issuer output never reaches disk or this wrapper's stdout/stderr. Credentials,
request bodies, query strings, user identifiers and arbitrary messages are omitted.
"""
import argparse
import datetime
import json
import os
import signal
import stat
import subprocess

MAX_BYTES = 256 * 1024
MAX_LINE = 64 * 1024
ROUTES = frozenset(('/api/oidc/token', '/api/oidc/authorization',
                    '/api/oidc/introspection', '/api/oidc/consent'))
MARKERS = ('invalid_client', 'invalid_grant', 'invalid_request', 'invalid_scope',
           'unauthorized_client', 'unsupported_grant_type', 'client_secret_basic',
           'client_secret_post', 'code_verifier', 'code_challenge', 'pkce',
           'redirect_uri', 'expired', 'signature', 'not allowed', 'not permitted',
           'authentication failed', 'malformed', 'resource', 'timeout')


def classify(raw):
    try:
        item = json.loads(raw)
    except (ValueError, UnicodeError):
        return None
    if not isinstance(item, dict):
        return None
    level = item.get('level')
    if level not in ('warn', 'warning', 'error', 'fatal', 'panic'):
        return None
    # Only marker presence is retained, never the original field values.
    searchable = ' '.join(str(item.get(k, '')) for k in ('msg', 'error')).lower()
    route = str(item.get('path', '')).split('?', 1)[0]
    return {'event': 'issuer_warning', 'level': level,
            'route': route if route in ROUTES else 'other',
            'markers': [m for m in MARKERS if m in searchable]}


def record(destination, event):
    flags = os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW
    fd = os.open(destination, flags, 0o600)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise ValueError('diagnostic destination must be a private owned regular file')
        event = dict(event, utc=datetime.datetime.now(datetime.timezone.utc).isoformat())
        encoded = (json.dumps(event, sort_keys=True) + '\n').encode()
        if info.st_size + len(encoded) > MAX_BYTES:
            os.ftruncate(fd, 0)
        os.write(fd, encoded)
    finally:
        os.close(fd)


def supervise(destination, command):
    child = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    def forward(signum, _frame):
        if child.poll() is None:
            child.send_signal(signum)
    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(sig, forward)
    def emit(event):
        try:
            record(destination, event)
        except (OSError, ValueError):
            # Diagnostics must not change issuer authorization or availability.
            pass
    emit({'event': 'issuer_started'})
    assert child.stdout is not None
    while True:
        raw = child.stdout.readline(MAX_LINE + 1)
        if not raw:
            break
        if len(raw) > MAX_LINE or not raw.endswith(b'\n'):
            while raw and not raw.endswith(b'\n'):
                raw = child.stdout.readline(MAX_LINE + 1)
            emit({'event': 'oversized_or_incomplete_line_omitted'})
            continue
        event = classify(raw)
        if event is not None:
            emit(event)
    result = child.wait()
    emit({'event': 'issuer_exited', 'exit_code': result})
    return result if result >= 0 else 128 - result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    if not command:
        parser.error('issuer command required')
    return supervise(args.output, command)


if __name__ == '__main__':
    raise SystemExit(main())
