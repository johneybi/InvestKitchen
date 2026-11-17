"""Loopback-only, bounded duplicate suppression for confidential OAuth refresh.

Never persists token responses or request material. The issuer still rotates and
validates refresh tokens. A duplicate response is served only while the issued
access token remains active according to a fresh issuer introspection call.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import http.client
import json
import math
import secrets
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlencode, urlsplit, quote_plus

LIMIT = 65536
WINDOW = 60.0
CAPACITY = 32


@dataclass
class Response:
    status: int
    body: bytes


def error(status: int, code: str) -> Response:
    return Response(status, json.dumps({'error': code}).encode())


@dataclass
class Entry:
    ready: threading.Event = field(default_factory=threading.Event)
    response: Response | None = None
    completed: float | None = None


class RefreshGate:
    def __init__(self, issuer, client_id: str, *, clock=time.monotonic,
                 wall_clock=time.time, window=WINDOW, capacity=CAPACITY):
        if not 0 < window <= WINDOW or not 0 < capacity <= CAPACITY:
            raise ValueError('invalid bounds')
        self.issuer = issuer
        self.client_id = client_id
        self.clock, self.wall_clock = clock, wall_clock
        self.window, self.capacity = window, capacity
        self.key = secrets.token_bytes(32)
        self.entries: dict[bytes, Entry] = {}
        self.lock = threading.Lock()
        self.counts = {'refresh_exchanges': 0, 'duplicate_requests': 0,
                       'verified_responses': 0, 'rejected_responses': 0}

    def count(self, name):
        with self.lock:
            self.counts[name] += 1

    def health(self):
        with self.lock:
            return dict(status='ready', **self.counts)

    def _prune_locked(self):
        now = self.clock()
        for key, entry in list(self.entries.items()):
            if entry.completed is not None and now - entry.completed >= self.window:
                del self.entries[key]

    def prune(self):
        with self.lock:
            self._prune_locked()

    def handle(self, body: bytes, authorization: str = '') -> Response:
        if len(body) > LIMIT:
            return error(413, 'invalid_request')
        try:
            pairs = parse_qsl(body.decode('utf8'), keep_blank_values=True,
                              strict_parsing=True, max_num_fields=32)
            if len({k for k, _ in pairs}) != len(pairs):
                return error(400, 'invalid_request')
            form = dict(pairs)
        except (UnicodeError, ValueError):
            return error(400, 'invalid_request')
        # Only the configured confidential client_secret_post refresh flow is
        # eligible. Authorization codes and other clients remain issuer-owned.
        eligible = (form.get('grant_type') == 'refresh_token'
                    and form.get('client_id') == self.client_id
                    and bool(form.get('client_secret'))
                    and bool(form.get('refresh_token')) and not authorization)
        if not eligible:
            return self.issuer.exchange(body, authorization)
        canonical = json.dumps(sorted(pairs), ensure_ascii=True).encode()
        key = hmac.digest(self.key, canonical, hashlib.sha256)
        with self.lock:
            self._prune_locked()
            entry = self.entries.get(key)
            owner = entry is None
            if owner:
                # Never evict in-flight exchanges: a second upstream refresh
                # could invalidate the grant while the first response is lost.
                if len(self.entries) >= self.capacity:
                    return error(503, 'temporarily_unavailable')
                entry = Entry()
                self.entries[key] = entry
        assert entry is not None
        if owner:
            self.count('refresh_exchanges')
            try:
                entry.response = self.issuer.exchange(body, authorization)
            except Exception:
                entry.response = error(503, 'temporarily_unavailable')
            finally:
                with self.lock:
                    entry.completed = self.clock()
                    entry.ready.set()
        else:
            self.count('duplicate_requests')
            if not entry.ready.wait(120):
                return error(503, 'temporarily_unavailable')
        assert entry.response is not None
        response = entry.response
        if response.status != 200:
            # Retain ambiguous failures briefly too; never automatically submit
            # a refresh again after an upstream response was lost.
            return response
        try:
            payload = json.loads(response.body)
            token = payload['access_token']
            if (not isinstance(token, str) or not token or
                    str(payload.get('token_type', '')).lower() != 'bearer' or
                    not isinstance(payload.get('refresh_token'), str)):
                raise ValueError('invalid token response')
            live = self.issuer.introspect(token, form['client_id'], form['client_secret'])
            if live.get('active') is not True or live.get('client_id') != self.client_id:
                # Do not bypass issuer revocation or extend duplicate tolerance
                # after a subsequent rotation. Return no token material.
                self.count('rejected_responses')
                return error(400, 'invalid_grant')
            exp = live.get('exp')
            if isinstance(exp, bool) or not isinstance(exp, (int, float)):
                raise ValueError('missing expiration')
            remaining = math.floor(exp - self.wall_clock())
            original = payload.get('expires_in')
            if isinstance(original, bool) or not isinstance(original, (int, float)):
                raise ValueError('missing lifespan')
            if remaining <= 0 or original <= 0:
                self.count('rejected_responses')
                return error(400, 'invalid_grant')
            payload['expires_in'] = min(remaining, math.floor(original))
            self.count('verified_responses')
            return Response(200, json.dumps(payload, separators=(',', ':')).encode())
        except Exception:
            return error(503, 'temporarily_unavailable')


class LoopbackIssuer:
    def __init__(self, port: int, public_origin: str):
        parsed = urlsplit(public_origin)
        if (parsed.scheme != 'https' or not parsed.hostname or parsed.username or
                parsed.password or parsed.path not in ('', '/') or parsed.query or parsed.fragment):
            raise ValueError('invalid public origin')
        self.port, self.authority = port, parsed.netloc

    def post(self, path: str, body: bytes, authorization='') -> Response:
        conn = http.client.HTTPConnection('127.0.0.1', self.port, timeout=110)
        headers = {'Content-Type': 'application/x-www-form-urlencoded',
                   'Host': self.authority, 'X-Forwarded-Host': self.authority,
                   'X-Forwarded-Proto': 'https', 'X-Forwarded-Port': '443'}
        if authorization:
            headers['Authorization'] = authorization
        try:
            conn.request('POST', path, body, headers)
            response = conn.getresponse()
            data = response.read(LIMIT + 1)
            if len(data) > LIMIT:
                return error(502, 'server_error')
            return Response(response.status, data)
        except (OSError, http.client.HTTPException):
            return error(503, 'temporarily_unavailable')
        finally:
            conn.close()

    def exchange(self, body, authorization=''):
        return self.post('/api/oidc/token', body, authorization)

    def introspect(self, token, client_id, client_secret):
        credentials = quote_plus(client_id) + ':' + quote_plus(client_secret)
        response = self.post('/api/oidc/introspection', urlencode({
            'token': token, 'token_type_hint': 'access_token'}).encode(),
            'Basic ' + base64.b64encode(credentials.encode()).decode())
        if response.status != 200:
            raise ValueError('introspection unavailable')
        return json.loads(response.body)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, address, gate):
        if address[0] != '127.0.0.1':
            raise ValueError('loopback listener required')
        self.gate = gate
        self.slots = threading.BoundedSemaphore(8)
        super().__init__(address, Handler)

    def service_actions(self):
        self.gate.prune()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.reply(Response(200, json.dumps(self.server.gate.health()).encode()) if self.path == '/healthz'
                   else error(404, 'not_found'))

    def do_POST(self):
        if self.path != '/api/oidc/token':
            self.reply(error(404, 'not_found'))
            return
        if (self.headers.get_content_type() != 'application/x-www-form-urlencoded'
                or self.headers.get('Transfer-Encoding') is not None):
            self.reply(error(400, 'invalid_request'))
            return
        try:
            values = self.headers.get_all('Content-Length', [])
            if len(values) != 1 or len(self.headers.get_all('Authorization', [])) > 1:
                raise ValueError()
            length = int(values[0])
            if not 0 < length <= LIMIT:
                raise ValueError()
        except ValueError:
            self.reply(error(400, 'invalid_request'))
            return
        if not self.server.slots.acquire(blocking=False):
            self.reply(error(503, 'temporarily_unavailable'))
            return
        try:
            self.connection.settimeout(10)
            body = self.rfile.read(length)
            if len(body) != length:
                result = error(400, 'invalid_request')
            else:
                result = self.server.gate.handle(body, self.headers.get('Authorization', ''))
            self.reply(result)
        except (OSError, ValueError):
            self.reply(error(503, 'temporarily_unavailable'))
        finally:
            self.server.slots.release()

    def reply(self, response):
        try:
            self.send_response(response.status)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Content-Length', str(len(response.body)))
            if response.status == 503:
                self.send_header('Retry-After', '5')
            self.end_headers()
            self.wfile.write(response.body)
        except OSError:
            # The exchange has already been retained even if the client left.
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, required=True)
    parser.add_argument('--issuer-port', type=int, required=True)
    parser.add_argument('--public-origin', required=True)
    parser.add_argument('--client-id', required=True)
    args = parser.parse_args()
    gate = RefreshGate(LoopbackIssuer(args.issuer_port, args.public_origin), args.client_id)
    Server(('127.0.0.1', args.port), gate).serve_forever()


if __name__ == '__main__':
    main()
