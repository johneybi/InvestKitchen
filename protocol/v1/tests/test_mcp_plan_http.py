from __future__ import annotations

import json
import time

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
def runtime(tmp_path, signing_key):
    raw = _config().model_dump()
    for grant in raw['grants']:
        grant['permissions'] = [*grant['permissions'], 'plan.read']
    resource = OAuthResourceConfig.model_validate(raw)
    config = ConfirmedWriteConfig.model_validate({
        'authelia_authz_url': 'http://127.0.0.1:19091/api/authz/forward-auth',
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
            write_principal=identity, write_grant=authority)
        gateway.portfolio_ids = ['portfolio-alpha', 'portfolio-beta']
        backends[grant.subject] = gateway
        return gateway
    now = [time.time()]
    writes = ConfirmedWriteSurface(resource, config, factory, audit_root=tmp_path / 'browser-evidence',
        browser_identity=lambda request: request.cookies.get('fixture-session'), clock=lambda: now[0])
    verifier = JWTTokenVerifier(resource, _jwks(signing_key))
    with TestClient(create_http_app(backends['fixture-reader'], verifier, writes=writes),
                    base_url='https://mcp.example.test') as client:
        headers = {**HEADERS, 'Authorization': 'Bearer ' + _jwt(signing_key, scope='investkitchen.read ' + WRITE_SCOPE)}
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
