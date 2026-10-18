from __future__ import annotations

import json
import time
from datetime import datetime, timezone

import pytest
from starlette.testclient import TestClient

from protocol.v1.runtime.reference_composition import build_reference_gateway
from protocol.v1.security.oauth_resource import JWTTokenVerifier, OAuthResourceConfig
from protocol.v1.tests.test_advisory_plan_update import ROOT, plan_request
from protocol.v1.tests.test_mcp_http import HEADERS, _config, _context, _jwks, _jwt, _post, signing_key
from protocol.v1.tests.test_mcp_confirmed_writes import _approve, _browser_form, _call
from protocol.v1.transport.mcp_confirmed_writes import ConfirmedWriteConfig, ConfirmedWriteSurface, WRITE_SCOPE
from protocol.v1.transport.mcp_http import create_http_app


@pytest.fixture
def runtime(tmp_path, signing_key, request, monkeypatch):
    now = [time.time()]
    clock = lambda: datetime.fromtimestamp(now[0], timezone.utc)
    monkeypatch.setattr('protocol.v1.runtime.advisory_plan_update._now', clock)
    monkeypatch.setattr('protocol.v1.security.trusted_approval._utc_now', lambda value: value or clock())
    raw = _config().model_dump()
    for grant in raw['grants']:
        grant['permissions'] = [*grant['permissions'], 'plan.read']
    resource = OAuthResourceConfig.model_validate(raw)
    config = ConfirmedWriteConfig.model_validate({
        'authelia_authz_url': 'http://127.0.0.1:19091/api/authz/forward-auth',
        'plan_preview_ttl_seconds': getattr(request, 'param', 300),
        'grants': [{**{k: getattr(grant, k) for k in ['subject', 'client_id', 'portfolio_scope']},
                    'browser_username': 'fixture-owner' if index == 0 else 'fixture-other',
                    'permissions': ['plan.update'], 'expires_at': '2099-01-01T00:00:00Z'}
                   for index, grant in enumerate(resource.grants)],
    })
    backends = {}
    def factory(grant):
        identity = {'subject_user_id': grant.subject, 'client_id': grant.client_id,
                    'credential_binding_id': 'fixture:' + grant.subject,
                    'expires_at': {'value': grant.expires_at.isoformat(), 'precision': 'source_exact'}}
        authority = {**identity, 'grant_id': 'grant:' + grant.subject,
                     'permissions': [*grant.permissions, 'operation.approve'],
                     'portfolio_scope': list(grant.portfolio_scope)}
        gateway = build_reference_gateway(
            runtime_root=ROOT, manifest=ROOT / 'protocol/v1/fixtures/full-reference.instance.json',
            native_store_root=tmp_path / 'native', advisory_plan_root=tmp_path / 'plans',
            approval_store_root=tmp_path / 'approvals' / grant.subject,
            plan_preview_ttl_seconds=config.plan_preview_ttl_seconds,
            write_principal=identity, write_grant=authority)
        gateway.portfolio_ids = ['portfolio-alpha', 'portfolio-beta']
        backends[grant.subject] = gateway
        return gateway
    writes = ConfirmedWriteSurface(resource, config, factory, audit_root=tmp_path / 'browser-evidence',
        browser_identity=lambda request: request.cookies.get('fixture-session'), clock=lambda: now[0])
    verifier = JWTTokenVerifier(resource, _jwks(signing_key))
    with TestClient(create_http_app(backends['fixture-reader'], verifier, writes=writes),
                    base_url='https://mcp.example.test') as client:
        headers = {**HEADERS, 'Authorization': 'Bearer ' + _jwt(signing_key, exp=int(time.time()) + 3600,
                                                              scope='investkitchen.read ' + WRITE_SCOPE)}
        yield client, headers, writes, now, backends, signing_key, tmp_path


def preview(client, headers, operation='create', plan_id=None):
    result = _call(client, headers, 'preview_plan_update', {'update': plan_request(operation, plan_id)})
    assert not result.get('isError'), result
    return result['structuredContent']


def apply(client, headers, review):
    return _call(client, headers, 'apply_plan_update', {key: review[key] for key in ['preview_id', 'confirmation']})


def test_gemini_plan_lifecycle_requires_independent_human_approval_and_requeries(runtime):
    client, headers, writes, _, backends, _, tmp_path = runtime
    gateway = backends['fixture-reader']
    tools = {t['name']: t for t in _post(client, 'tools/list', headers=headers).json()['result']['tools']}
    assert tools['preview_plan_update']['annotations']['readOnlyHint'] is True
    assert tools['apply_plan_update']['annotations']['readOnlyHint'] is False
    assert 'apply_portfolio_update' not in tools and 'apply_knowledge_update' not in tools
    assert 'get_active_plans' in tools and 'get_plan_history' in tools
    current = None
    for operation in ['create', 'replace', 'close']:
        review = preview(client, headers, operation, current)
        # The exact model-visible confirmation phrase alone cannot authorize a write.
        assert apply(client, headers, review)['isError'] is True
        path, _ = _browser_form(client, review)
        assert '조건부 Plan 생성·교체·종료' in client.get(path).text
        _approve(client, review)
        result = apply(client, headers, review)['structuredContent']
        assert result['status'] == 'applied' and result['read_back']['matches'] is True
        current = result['plan_id']
        active = _call(client, headers, 'get_active_plans', {'portfolio_id': 'portfolio-alpha'})['structuredContent']
        assert len(active['data']['plans']) == (0 if operation == 'close' else 1)
        assert apply(client, headers, review)['isError'] is True
    history = _call(client, headers, 'get_plan_history', {'portfolio_id': 'portfolio-alpha'})['structuredContent']
    assert history['total_events'] == 3
    assert 'approval_id' not in json.dumps(history) and str(tmp_path) not in json.dumps(history)
    audit = [json.loads(line) for line in (writes.audit_root / 'browser-confirmations.jsonl').read_text().splitlines()]
    assert len(audit) == 3 and all(row['event'] == 'authenticated_browser_approve' for row in audit)
    assert not list((tmp_path / 'native').glob('*journal.jsonl'))


def test_plan_scope_client_identity_and_schema_are_enforced(runtime):
    client, headers, _, _, backends, key, _ = runtime
    other = {**headers, 'Authorization': 'Bearer ' + _jwt(key, sub='fixture-other', scope='investkitchen.read ' + WRITE_SCOPE)}
    for name, args, auth in [
        ('preview_plan_update', {'update': plan_request(portfolio_id='portfolio-beta')}, headers),
        ('get_plan_history', {'portfolio_id': 'portfolio-alpha'}, other),
        ('get_active_plans', {'portfolio_id': 'portfolio-beta'}, headers),
    ]:
        assert _call(client, auth, name, args)['isError'] is True
    review = preview(client, headers)
    _approve(client, review)
    assert apply(client, other, review)['isError'] is True
    injected = {key: review[key] for key in ['preview_id', 'confirmation']}
    injected['principal'] = {'subject_user_id': 'forged'}
    assert _call(client, headers, 'apply_plan_update', injected)['isError'] is True
    assert not backends['fixture-reader'].plan_store.native_path.exists()
    assert apply(client, headers, review)['structuredContent']['status'] == 'applied'


def test_plan_write_scope_never_creates_an_operator_permission(runtime):
    client, headers, writes, _, _, key, _ = runtime
    readonly = {**headers, 'Authorization': 'Bearer ' + _jwt(key)}
    names = {t['name'] for t in _post(client, 'tools/list', headers=readonly).json()['result']['tools']}
    assert 'get_active_plans' in names and 'apply_plan_update' not in names
    assert _call(client, readonly, 'preview_plan_update', {'update': plan_request()})['isError'] is True
    denied = _post(client, 'tools/list', headers=HEADERS)
    assert 'scope="investkitchen.read investkitchen.write"' in denied.headers['WWW-Authenticate']
    caps = _call(client, headers, 'get_capabilities', {})['structuredContent']
    assert caps['remote_write_tools'] == ['apply_plan_update', 'preview_plan_update']
    assert caps['write_approval'] == 'separate_authenticated_browser_review'
    assert writes.config.grants[0].permissions == ('plan.update',)
    # A write-scoped token against a read-only server cannot install a writer.
    verifier = JWTTokenVerifier(writes.resource, _jwks(key))
    gateway = runtime[4]['fixture-reader']
    with TestClient(create_http_app(gateway, verifier), base_url='https://mcp.example.test') as reader:
        listed = {tool['name'] for tool in _post(reader, 'tools/list', headers=headers).json()['result']['tools']}
        assert 'apply_plan_update' not in listed
        assert _call(reader, headers, 'preview_plan_update', {'update': plan_request()})['isError'] is True


def test_composed_plan_reads_cannot_cross_portfolio_scope(runtime):
    client, headers, _, _, _, _, _ = runtime
    args = _context(requested_context=['plan.active'])
    args['capability_inputs'] = {'plan.active': {'portfolio_id': 'portfolio-beta'}}
    assert _call(client, headers, 'build_decision_context', args)['isError'] is True
    args['capability_inputs']['plan.active']['portfolio_id'] = 'portfolio-alpha'
    assert not _call(client, headers, 'build_decision_context', args).get('isError')


@pytest.mark.parametrize('failure', ['reject', 'wrong-browser', 'expiry', 'audit-unavailable'])
def test_plan_browser_approval_failures_do_not_persist(runtime, monkeypatch, failure):
    client, headers, writes, now, backends, _, _ = runtime
    review = preview(client, headers)
    if failure == 'wrong-browser':
        client.cookies.set('fixture-session', 'fixture-other')
        from urllib.parse import urlsplit
        assert client.get(urlsplit(review['approval_url']).path).status_code == 404
    else:
        path, form = _browser_form(client, review)
        if failure == 'reject':
            form['decision'] = 'reject'
        elif failure == 'expiry':
            now[0] += 301
        else:
            def fail(*args):
                raise OSError('synthetic unavailable approval evidence')
            monkeypatch.setattr(writes, '_audit', fail)
        response = client.post(path, data=form, headers={'Origin': 'https://mcp.example.test'})
        assert response.status_code == {'reject': 200, 'expiry': 404, 'audit-unavailable': 503}[failure]
    assert apply(client, headers, review)['isError'] is True
    assert not backends['fixture-reader'].plan_store.native_path.exists()


@pytest.mark.parametrize('runtime', [900], indirect=True)
def test_configured_plan_window_allows_delayed_review_but_never_extends_deadline(runtime):
    client, headers, writes, now, backends, _, _ = runtime
    review = preview(client, headers)
    candidate = review['candidate']
    start = datetime.fromisoformat(candidate['prepared_at']['value'].replace('Z', '+00:00')).timestamp()
    end = datetime.fromisoformat(candidate['expires_at']['value'].replace('Z', '+00:00')).timestamp()
    assert end - start == 900
    assert next(iter(writes.pending.values()))['expires'] == pytest.approx(end, abs=0.000001, rel=0)
    now[0] += 360  # Model response + independent human login exceed the old limit.
    assert apply(client, headers, review)['isError'] is True
    _approve(client, review)
    result = apply(client, headers, review)['structuredContent']
    assert result['status'] == 'applied' and result['read_back']['matches'] is True
    replacement = preview(client, headers, 'replace', result['plan_id'])
    now[0] += 850
    _approve(client, replacement)
    now[0] += 51  # Browser approval does not restart the configured window.
    assert apply(client, headers, replacement)['isError'] is True
    assert len(backends['fixture-reader'].plan_store.snapshot()['history']) == 1


@pytest.mark.parametrize('runtime', [900], indirect=True)
def test_client_cannot_override_plan_review_window(runtime):
    client, headers, _, _, backends, _, _ = runtime
    for args in [{'update': plan_request(), 'ttl_seconds': 3600},
                 {'update': {**plan_request(), 'plan_preview_ttl_seconds': 3600}}]:
        assert _call(client, headers, 'preview_plan_update', args)['isError'] is True
    assert not backends['fixture-reader'].plan_store.native_path.exists()


@pytest.mark.parametrize('value', [0, 59, 901, True, '900', 900.5])
def test_plan_review_window_config_rejects_unbounded_or_coerced_values(value):
    from protocol.v1.tests.test_mcp_confirmed_writes import _write_config
    with pytest.raises(ValueError):
        _write_config(plan_preview_ttl_seconds=value)


def test_plan_review_window_default_is_unchanged():
    from protocol.v1.tests.test_mcp_confirmed_writes import _write_config
    assert _write_config().plan_preview_ttl_seconds == 300


def test_browser_deadline_never_outlives_shorter_canonical_preview(runtime):
    client, headers, writes, now, backends, _, _ = runtime
    # A separately composed backend may still use the default five-minute TTL.
    writes.config = writes.config.model_copy(update={'plan_preview_ttl_seconds': 900})
    review = preview(client, headers)
    end = datetime.fromisoformat(review['candidate']['expires_at']['value'].replace('Z', '+00:00')).timestamp()
    assert next(iter(writes.pending.values()))['expires'] <= end
    now[0] += 301
    assert apply(client, headers, review)['isError'] is True
    assert not backends['fixture-reader'].plan_store.native_path.exists()


@pytest.mark.parametrize('runtime', [900], indirect=True)
def test_browser_deadline_never_outlives_write_grant(runtime):
    client, headers, writes, now, backends, _, _ = runtime
    grant = writes.config.grants[0].model_copy(update={'expires_at': datetime.fromtimestamp(now[0] + 120, timezone.utc)})
    writes.config = writes.config.model_copy(update={'grants': (grant, *writes.config.grants[1:])})
    review = preview(client, headers)
    assert next(iter(writes.pending.values()))['expires'] == grant.expires_at.timestamp()
    _approve(client, review)
    now[0] += 121
    assert apply(client, headers, review)['isError'] is True
    assert not backends['fixture-reader'].plan_store.native_path.exists()
