import concurrent.futures
import json
import http.client
import threading
from urllib.parse import parse_qs, urlencode

import pytest

from deploy.gemini.oauth_refresh_proxy import RefreshGate, Response, Server


def body(**extra):
    return urlencode(dict(grant_type='refresh_token', client_id='synthetic-client',
                          client_secret='synthetic-secret', refresh_token='old', **extra)).encode()


class Issuer:
    """Synthetic rotation: reusing old refresh revokes the entire test grant."""
    def __init__(self):
        self.calls = 0
        self.live = True
        self.introspection_calls = 0
        self.exp = 1300
        self.client = 'synthetic-client'
        self.introspection_error = False

    def exchange(self, request, authorization=''):
        self.calls += 1
        if self.calls > 1:
            self.live = False
            return Response(400, b'{"error":"invalid_grant"}')
        return Response(200, json.dumps(dict(access_token='new-access', refresh_token='new-refresh',
                                            token_type='Bearer', expires_in=300)).encode())

    def introspect(self, token, client_id, secret):
        self.introspection_calls += 1
        if self.introspection_error:
            raise OSError()
        return dict(active=self.live, client_id=self.client, exp=self.exp)


def gate(issuer=None, **kwargs):
    return RefreshGate(issuer or Issuer(), 'synthetic-client', wall_clock=lambda: 1000, **kwargs)


def test_lost_success_response_retry_does_not_revoke_rotated_grant():
    issuer = Issuer()
    proxy = gate(issuer)
    first = proxy.handle(body())  # discard this response as a disconnected client would
    second = proxy.handle(body())
    assert first == second
    assert second.status == 200 and issuer.calls == 1 and issuer.live
    assert issuer.introspection_calls == 2


def test_stage_diagnostics_distinguish_slow_exchange_and_introspection():
    now = [0]
    issuer = Issuer()
    exchange, introspect = issuer.exchange, issuer.introspect
    def slow_exchange(*args):
        now[0] += 25
        return exchange(*args)
    def slow_introspect(*args):
        now[0] += 4
        return introspect(*args)
    issuer.exchange, issuer.introspect = slow_exchange, slow_introspect
    proxy = gate(issuer, clock=lambda: now[0])
    assert proxy.handle(body()).status == 200
    stages = proxy.health()['recent_stages']
    assert [(x['stage'], x['status'], x['elapsed_ms']) for x in stages] == [
        ('exchange', 200, 25000), ('introspection', 200, 4000)]
    encoded = json.dumps(proxy.health())
    assert all(secret not in encoded for secret in ('synthetic-secret', 'new-access', 'new-refresh'))


def test_diagnostics_are_bounded_copied_and_do_not_store_error_text():
    issuer = Issuer()
    issuer.introspection_error = True
    proxy = gate(issuer)
    for _ in range(30):
        assert proxy.handle(body()).status == 503
    stages = proxy.health()['recent_stages']
    assert len(stages) == 16
    assert all(x['stage'] == 'introspection' and x['status'] == 0 for x in stages)
    stages[0]['stage'] = 'mutated'
    assert proxy.health()['recent_stages'][0]['stage'] == 'introspection'
    assert proxy.counts['refresh_exchanges'] == 1


def test_without_duplicate_suppression_same_retry_revokes_grant():
    issuer = Issuer()
    assert issuer.exchange(body()).status == 200
    assert issuer.exchange(body()).status == 400
    assert not issuer.live


def test_concurrent_refresh_is_coalesced():
    issuer = Issuer()
    started, release = threading.Event(), threading.Event()
    original = issuer.exchange
    def delayed(*args):
        started.set()
        assert release.wait(3)
        return original(*args)
    issuer.exchange = delayed
    proxy = gate(issuer)
    with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
        first = executor.submit(proxy.handle, body())
        assert started.wait(2)
        other = [executor.submit(proxy.handle, body()) for _ in range(4)]
        release.set()
        assert all(f.result().status == 200 for f in [first, *other])
    assert issuer.calls == 1


def test_window_is_absolute_and_does_not_slide_on_reads():
    now = [0]
    issuer = Issuer()
    proxy = gate(issuer, clock=lambda: now[0])
    assert proxy.handle(body()).status == 200
    now[0] = 59
    assert proxy.handle(body()).status == 200
    now[0] = 60
    assert proxy.handle(body()).status == 400
    assert issuer.calls == 2


def test_idle_maintenance_discards_expired_response_material():
    now = [0]
    proxy = gate(clock=lambda: now[0])
    assert proxy.handle(body()).status == 200
    assert len(proxy.entries) == 1
    now[0] = 60
    proxy.prune()
    assert not proxy.entries


@pytest.mark.parametrize('change', ['secret', 'resource', 'scope', 'refresh'])
def test_changed_request_cannot_receive_cached_token(change):
    issuer = Issuer()
    proxy = gate(issuer)
    assert proxy.handle(body()).status == 200
    form = {k: v[0] for k, v in parse_qs(body().decode()).items()}
    form[{'secret':'client_secret', 'resource':'resource', 'scope':'scope', 'refresh':'refresh_token'}[change]] = 'different'
    assert proxy.handle(urlencode(form).encode()).status == 400
    assert issuer.calls == 2


def test_equivalent_parameter_order_is_same_request():
    issuer = Issuer()
    proxy = gate(issuer)
    assert proxy.handle(body()).status == 200
    assert proxy.handle(b'&'.join(reversed(body().split(b'&')))).status == 200
    assert issuer.calls == 1


@pytest.mark.parametrize('condition', ['revoked', 'expired', 'wrong_client', 'unavailable'])
def test_cached_response_requires_live_introspection(condition):
    issuer = Issuer()
    proxy = gate(issuer)
    assert proxy.handle(body()).status == 200
    if condition == 'revoked': issuer.live = False
    if condition == 'expired': issuer.exp = 999
    if condition == 'wrong_client': issuer.client = 'different'
    if condition == 'unavailable': issuer.introspection_error = True
    result = proxy.handle(body())
    assert result.status in (400, 503)
    assert b'new-access' not in result.body and b'new-refresh' not in result.body
    assert issuer.introspection_calls == 2


def test_retried_response_reduces_remaining_lifetime():
    now = [1000]
    proxy = RefreshGate(Issuer(), 'synthetic-client', wall_clock=lambda: now[0])
    assert json.loads(proxy.handle(body()).body)['expires_in'] == 300
    now[0] += 40
    assert json.loads(proxy.handle(body()).body)['expires_in'] == 260


def test_introspection_outage_recovers_without_repeating_rotation():
    issuer = Issuer()
    issuer.introspection_error = True
    proxy = gate(issuer)
    assert proxy.handle(body()).status == 503
    issuer.introspection_error = False
    assert proxy.handle(body()).status == 200
    assert issuer.calls == 1


def test_ambiguous_exchange_failure_is_not_automatically_retried():
    issuer = Issuer()
    def failed(*args):
        issuer.calls += 1
        raise TimeoutError()
    issuer.exchange = failed
    proxy = gate(issuer)
    assert proxy.handle(body()).status == proxy.handle(body()).status == 503
    assert issuer.calls == 1


def test_capacity_fails_closed_without_evicting_valid_entry():
    issuer = Issuer()
    proxy = gate(issuer, capacity=1)
    assert proxy.handle(body()).status == 200
    assert proxy.handle(body(scope='different')).status == 503
    assert proxy.handle(body()).status == 200
    assert issuer.calls == 1


@pytest.mark.parametrize('raw_request', [b'grant_type', b'\xff', body()+b'&client_id=other', b'x=' + b'a'*65536])
def test_malformed_or_oversized_requests_never_reach_issuer(raw_request):
    issuer = Issuer()
    assert gate(issuer).handle(raw_request).status in (400, 413)
    assert issuer.calls == 0


def test_restart_has_no_replayable_memory():
    issuer = Issuer()
    assert gate(issuer).handle(body()).status == 200
    assert gate(issuer).handle(body()).status == 400


def test_noneligible_flow_is_not_cached():
    issuer = Issuer()
    proxy = gate(issuer)
    request = b'grant_type=authorization_code&code=synthetic'
    assert proxy.handle(request).status == 200
    assert proxy.handle(request).status == 400
    assert not proxy.entries and issuer.introspection_calls == 0


def test_listener_cannot_bind_publicly():
    with pytest.raises(ValueError):
        Server(('0.0.0.0', 0), gate())


def test_real_listener_restarts_after_server_closed_health_connection():
    import socket
    first = Server(('127.0.0.1', 0), gate())
    address = first.server_address
    thread = threading.Thread(target=first.serve_forever, daemon=True)
    thread.start()
    try:
        # Wait for server EOF before closing our socket so the server is the
        # active closer and its endpoint enters TIME_WAIT.
        with socket.create_connection(address, timeout=3) as client:
            client.sendall(b'GET /healthz HTTP/1.0\r\nHost: localhost\r\n\r\n')
            while client.recv(4096):
                pass
    finally:
        first.shutdown(); first.server_close(); thread.join(timeout=3)
    second = Server(address, gate())
    try:
        assert not second.gate.entries
        # Address reuse must not let another process share a running listener.
        with pytest.raises(OSError):
            Server(address, gate())
    finally:
        second.server_close()


def test_real_http_boundary_deduplicates_and_never_exposes_health_public_path():
    issuer = Issuer()
    server = Server(('127.0.0.1', 0), gate(issuer))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    def request(method, path, data=None, headers=None):
        conn = http.client.HTTPConnection(*server.server_address, timeout=3)
        try:
            conn.request(method, path, data, headers or {})
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally: conn.close()
    try:
        headers = {'Content-Type': 'application/x-www-form-urlencoded'}
        for _ in range(2):
            status, returned, raw = request('POST', '/api/oidc/token', body(), headers)
            assert status == 200 and returned['Cache-Control'] == 'no-store'
            assert json.loads(raw)['access_token'] == 'new-access'
        assert issuer.calls == 1
        assert request('POST', '/other', body(), headers)[0] == 404
        assert request('POST', '/api/oidc/token', body(), {'Content-Type':'application/json'})[0] == 400
        assert request('GET', '/api/oidc/token')[0] == 404
        _, _, raw = request('GET', '/healthz')
        counts = json.loads(raw)
        assert counts['duplicate_requests'] == 1
        assert 'synthetic-secret' not in raw.decode() and 'new-access' not in raw.decode()
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=3)
