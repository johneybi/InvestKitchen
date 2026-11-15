"""Synthetic remote parity checks: no provider credentials or production state."""
import json
import time

import pytest
from mcp.server.auth.provider import AccessToken
from starlette.testclient import TestClient

from protocol.v1.security.oauth_resource import JWTTokenVerifier
from protocol.v1.transport.mcp_read_surface import RemoteReadSurface, ReadSurfaceError
from protocol.v1.transport.mcp_confirmed_writes import ConfirmedWriteConfig, ConfirmedWriteSurface
from protocol.v1.transport.mcp_http import create_http_app
from protocol.v1.tests.test_mcp_http import (
    ROOT, RESOURCE, READ_SCOPE, _config, _gateway, _jwt, _jwks, _post, HEADERS, signing_key,
)
from protocol.v1.tests.test_mcp_confirmed_writes import _approve, _call
from protocol.v1.tests.test_policy_opinion_update import _config as opinion_config
from protocol.v1.tests.test_personal_data_migration import _synthetic_bundle
from protocol.v1.tests.test_portfolio_update_service import _stores
from protocol.v1.runtime.reference_composition import build_reference_gateway
from protocol.v1.runtime.opinion_weighting import OpinionWeightingStore

EXTRA = ['openitem.read', 'policy.read', 'opinion.read', 'decision.history.read',
         'transaction.history.read', 'reflection.session.start']


def token():
    return AccessToken(token='synthetic', client_id='gemini-fixture', subject='fixture-reader',
                       scopes=[READ_SCOPE], resource=RESOURCE, expires_at=int(time.time()) + 300)


@pytest.mark.parametrize('tool,args', [
    ('get_open_items', {'portfolio_id': 'portfolio-beta'}),
    ('get_current_policy', {'portfolio_id': 'portfolio-beta'}),
    ('get_decision_history', {'portfolio_id': 'portfolio-beta'}),
    ('get_transactions', {'portfolio_id': 'portfolio-beta'}),
])
def test_new_reads_deny_other_portfolio_before_dispatch(tool, args):
    gateway = _gateway()
    # Supply synthetic ready capabilities so scope is exercised, not availability.
    from protocol.v1.transport.mcp_read_surface import READ_TOOLS
    cap = READ_TOOLS[tool]
    gateway.handlers[cap] = lambda _: pytest.fail('Unauthorized dispatch')
    surface = RemoteReadSurface(gateway, _config(EXTRA))
    assert tool in surface.tool_names(surface.grant(token()))
    with pytest.raises(ReadSurfaceError, match='portfolio is not authorized'):
        surface.call_tool(token(), tool, args)


def test_old_grants_cannot_discover_or_call_new_reads():
    surface = RemoteReadSurface(_gateway(), _config())
    for name in ('get_open_items', 'get_current_policy', 'get_opinion_weighting',
                 'build_opinion_consensus', 'get_decision_history', 'get_transactions', 'start_reflection'):
        assert name not in surface.tool_names(surface.grant(token()))
        with pytest.raises(ReadSurfaceError):
            surface.call_tool(token(), name, {})


@pytest.mark.parametrize('fmt', ['object', 'json-string'])
@pytest.mark.parametrize('kind', ['policy', 'opinion_weighting'])
def test_added_writes_require_browser_approval_and_readback(tmp_path, signing_key, fmt, kind):
    native, checkpoints = _stores(tmp_path)
    personal = _synthetic_bundle(tmp_path / 'personal')
    from protocol.v1.tests.test_policy_opinion_update import _policy_store
    seeded = _policy_store(tmp_path)
    advisory = tmp_path / "advisory-state"
    # Rebind the synthetic policy fixture to the authorized synthetic portfolio.
    root = advisory / "policies"
    rows = json.loads((root / "policies.json").read_text())
    for row in rows:
        row["portfolio_id"] = row["portfolio_id"].replace("portfolio-a", "portfolio-alpha").replace("portfolio-b", "portfolio-beta")
    from protocol.v1.adapters.common import canonical_json
    import hashlib
    raw = canonical_json(rows) + "\n"
    (root / "policies.json").write_text(raw)
    manifest = json.loads((root / "manifest.json").read_text())
    manifest["policies_sha256"] = hashlib.sha256(raw.encode()).hexdigest()
    manifest["summary"]["active_by_portfolio"] = {"portfolio-alpha": 1, "portfolio-beta": 1}
    (root / "manifest.json").write_text(canonical_json(manifest) + "\n")
    OpinionWeightingStore(advisory / 'opinion-weighting').initialize(opinion_config(), source_ref='fixture:initial')
    resource = _config(['system.capabilities.read', 'portfolio.read', 'knowledge.read', 'policy.read', 'opinion.read'])
    config = ConfirmedWriteConfig.model_validate({
        'authelia_authz_url': 'http://127.0.0.1:19091/api/authz/forward-auth',
        'grants': [{'subject': 'fixture-reader', 'client_id': 'gemini-fixture',
                    'browser_username': 'fixture-owner', 'portfolio_scope': ['portfolio-alpha'],
                    'permissions': ['policy.update', 'opinion.update'], 'expires_at': '2099-01-01T00:00:00Z'}]})
    backends = []
    def factory(grant):
        expiry = {'value': grant.expires_at.isoformat(), 'precision': 'source_exact'}
        identity = {'subject_user_id': grant.subject, 'client_id': grant.client_id,
                    'credential_binding_id': 'credential:synthetic'}
        gateway = build_reference_gateway(runtime_root=ROOT,
            manifest=ROOT / 'protocol/v1/fixtures/full-reference.instance.json',
            personal_data_root=personal, native_store_root=native.root,
            portfolio_checkpoint_root=checkpoints.root, advisory_state_root=advisory,
            approval_store_root=tmp_path / 'approvals',
            write_principal={**identity, 'expires_at': expiry},
            write_grant={**identity, 'grant_id': 'grant:synthetic', 'expires_at': expiry,
                         'portfolio_scope': list(grant.portfolio_scope),
                         'permissions': [*grant.permissions, 'operation.approve']})
        backends.append(gateway)
        return gateway
    writes = ConfirmedWriteSurface(resource, config, factory, audit_root=tmp_path / 'audit',
                                  browser_identity=lambda r: r.cookies.get('fixture-session'))
    backends[0].portfolio_ids = ["portfolio-alpha", "portfolio-beta"]
    app = create_http_app(backends[0], JWTTokenVerifier(resource, _jwks(signing_key)),
                          writes=writes, write_input_format=fmt)
    update = ({'portfolio_id': 'portfolio-alpha', 'rules_patch': {'account_operating_policy': {'irp': 'etf_centered_core'}},
               'source_ref': 'fixture:policy'} if kind == 'policy' else
              {'changes': {'user_preferences': {'speaker_multipliers': {'Analyst B': 1.2}}}, 'source_ref': 'fixture:opinion'})
    with TestClient(app, base_url='https://mcp.example.test') as client:
        headers = {**HEADERS, 'Authorization': 'Bearer ' + _jwt(signing_key, scope=READ_SCOPE + ' investkitchen.write')}
        args = {'update_json': json.dumps(update)} if fmt == 'json-string' else {'update': update}
        if kind == 'policy':
            bad = {**update, 'portfolio_id': 'portfolio-beta'}
            denied = {'update_json': json.dumps(bad)} if fmt == 'json-string' else {'update': bad}
            assert _call(client, headers, 'preview_policy_update', denied)['isError']
            assert not writes.pending
        response = _call(client, headers, 'preview_' + kind + '_update', args)
        assert not response.get('isError'), (response, writes.names(config.grants[0]))
        preview = response['structuredContent']
        assert preview['status'] == 'review_required' and preview['approval_url']
        apply_args = {key: preview[key] for key in ('preview_id', 'confirmation')}
        assert _call(client, headers, 'apply_' + kind + '_update', apply_args)['isError']
        _approve(client, preview)
        result = _call(client, headers, 'apply_' + kind + '_update', apply_args)['structuredContent']
        assert result['status'] == 'applied' and result['read_back']['matches']
        assert _call(client, headers, 'apply_' + kind + '_update', apply_args)['isError']
        writes.clock = lambda: 4102444801  # Grant expired: hide all write tools.
        assert writes.list_tools(token().model_copy(update={'scopes': [READ_SCOPE, 'investkitchen.write']})) == []


def test_context_policy_and_history_scope_cannot_bypass_direct_scope():
    gateway = _gateway()
    for capability in ('policy.current', 'decision.history', 'transaction.history', 'openitem.current'):
        gateway.handlers[capability] = lambda _: pytest.fail('Unauthorized context dispatch')
    surface = RemoteReadSurface(gateway, _config([*EXTRA, 'decision.context.build']))
    from protocol.v1.tests.test_mcp_http import _context
    for capability in ('policy.current', 'decision.history', 'transaction.history', 'openitem.current'):
        args = _context(requested_context=[capability])
        args['capability_inputs'] = {capability: {'portfolio_id': 'portfolio-beta'}}
        with pytest.raises(ReadSurfaceError, match='portfolio is not authorized'):
            surface.call_tool(token(), 'build_decision_context', args)


def test_ready_new_reads_are_callable_with_explicit_permissions(signing_key):
    gateway = _gateway()
    from protocol.v1.transport.mcp_read_surface import READ_TOOLS
    requests = {
        'get_open_items': {'portfolio_id': 'portfolio-alpha'},
        'get_current_policy': {'portfolio_id': 'portfolio-alpha'},
        'get_opinion_weighting': {},
        'build_opinion_consensus': {'question': 'Compare attributed synthetic views'},
        'get_decision_history': {'portfolio_id': 'portfolio-alpha'},
        'get_transactions': {'portfolio_id': 'portfolio-alpha'},
        'start_reflection': {'mode': 'pre'},
    }
    seen = []
    for name in requests:
        capability = READ_TOOLS[name]
        def handler(payload, capability=capability):
            seen.append(capability)
            return {'capability': capability, 'status': 'ok', 'data': {'synthetic': True}}
        gateway.handlers[capability] = handler
    permissions = [*EXTRA, 'knowledge.read']
    with TestClient(create_http_app(gateway, JWTTokenVerifier(_config(permissions), _jwks(signing_key))),
                    base_url='https://mcp.example.test') as client:
        headers = {**HEADERS, 'Authorization': 'Bearer ' + _jwt(signing_key)}
        listed = _post(client, 'tools/list', headers=headers).json()['result']['tools']
        assert set(requests).issubset({t['name'] for t in listed})
        for name, args in requests.items():
            result = _call(client, headers, name, args)
            assert not result.get('isError'), result
            assert result['structuredContent']['status'] == 'ok'
    assert len(seen) == len(requests)


def test_account_sync_stays_object_shaped_in_json_profile():
    from protocol.v1.transport.client_write_format import json_write_declaration
    from protocol.v1.transport.stdio_rpc import LocalToolRouter
    router = LocalToolRouter(_gateway())
    declaration = {'name': 'preview_account_sync', 'description': 'Synthetic',
                   'inputSchema': router.tools['preview_account_sync']['input_schema']}
    assert json_write_declaration(declaration) == declaration
