"""Standing Knowledge-addition authority, shared by both MCP transports."""
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
import pytest
from protocol.v1.tests.test_mcp_write_surface import ROOT, _principal, _grant, _call
from protocol.v1.tests.test_native_knowledge_store import _request
from protocol.v1.runtime.reference_composition import build_reference_gateway
from protocol.v1.runtime.native_knowledge_store import NativeKnowledgeStore
from protocol.v1.transport.mcp_stdio import TradeMindMCPServer
from protocol.v1.transport.stdio_rpc import LocalToolRouter


def update(generation='generation:registration'):
    value = _request(generation_id=generation)
    value['current_state'] = {}
    return value


def gateway(tmp_path, *, enabled=True, principal=None, grant=None):
    grant = deepcopy(grant or _grant())
    if enabled:
        grant['request_policy']['knowledge_registration'] = 'append_only'
    return build_reference_gateway(runtime_root=ROOT,
        manifest=ROOT/'protocol/v1/fixtures/full-reference.instance.json',
        native_store_root=tmp_path/'native', approval_store_root=tmp_path/'approval',
        write_principal=principal or _principal(), write_grant=grant)


def test_default_hidden_and_direct_call_denied(tmp_path):
    g = gateway(tmp_path, enabled=False)
    assert 'register_knowledge' not in LocalToolRouter(g).available_tool_names()
    assert g.register_knowledge(update())['status'] == 'blocked'
    assert not NativeKnowledgeStore(tmp_path/'native').has_generations()


def test_stdio_register_readback_and_restart_retry(tmp_path):
    g = gateway(tmp_path)
    result = _call(TradeMindMCPServer(g), 'register_knowledge', {'update': update()}, request_id='register')
    assert result['status'] == 'applied'
    assert result['read_back']['matches'] is True
    assert 'approval_url' not in result
    assert gateway(tmp_path).register_knowledge(update())['status'] == 'already_registered'
    assert len(NativeKnowledgeStore(tmp_path/'native').read_journal()) == 1
    journal = (tmp_path/'approval'/'approval-journal.jsonl').read_text()
    assert 'server_granted_knowledge_addition' in journal


@pytest.mark.parametrize('change', ['state', 'claim', 'evidence', 'generation', 'lifecycle'])
def test_replacements_and_state_fail_closed(tmp_path, change):
    g = gateway(tmp_path)
    assert g.register_knowledge(update())['status'] == 'applied'
    changed = update('generation:new')
    if change == 'state': changed['current_state'] = {'summary':'replace'}
    if change == 'claim': changed['claims'][0]['statement'] = 'replace'
    if change == 'evidence': changed['evidence'][0]['verification'] = 'unverified'
    if change == 'generation':
        changed['generation_id'] = update()['generation_id']
        changed['claims'][0]['statement'] = 'replace'
    if change == 'lifecycle': changed['claims'][0]['registration_state'] = 'superseded'
    assert g.register_knowledge(changed)['status'] == 'blocked'
    assert len(NativeKnowledgeStore(tmp_path/'native').read_journal()) == 1


def test_identical_entries_new_generation_is_noop(tmp_path):
    g = gateway(tmp_path)
    assert g.register_knowledge(update())['status'] == 'applied'
    assert g.register_knowledge(update('generation:another'))['status'] == 'already_present'
    assert len(NativeKnowledgeStore(tmp_path/'native').read_journal()) == 1


@pytest.mark.parametrize('invalid', ['identity', 'expiry', 'permission'])
def test_authority_is_not_model_confirmation(tmp_path, invalid):
    grant = _grant()
    if invalid == 'identity': grant['client_id'] = 'other'
    if invalid == 'expiry': grant['expires_at']['value'] = '2020-01-01T00:00:00Z'
    if invalid == 'permission': grant['permissions'].remove('operation.approve')
    g = gateway(tmp_path, grant=grant)
    assert g.register_knowledge(update())['status'] == 'blocked'
    assert not NativeKnowledgeStore(tmp_path/'native').has_generations()


def test_readback_failure_reports_applied_not_failed(tmp_path, monkeypatch):
    g = gateway(tmp_path)
    store = g.connector_review.__self__.native_knowledge
    original = store.get_generation
    count = [0]
    def fail_second(*args, **kwargs):
        count[0] += 1
        if count[0] == 2: raise OSError('synthetic')
        return original(*args, **kwargs)
    monkeypatch.setattr(store, 'get_generation', fail_second)
    result = g.register_knowledge(update())
    assert result['status'] == 'applied'
    assert result['read_back']['status'] == 'unavailable'
    assert len(store.read_journal()) == 1


def test_concurrent_writers_cannot_replace_same_id(tmp_path):
    first, second = gateway(tmp_path), gateway(tmp_path)
    other = update('generation:concurrent')
    other['claims'][0]['statement'] = 'different'
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda pair: pair[0].register_knowledge(pair[1]), [(first,update()), (second,other)]))
    assert sum(r['status'] == 'applied' for r in results) == 1
    assert len(NativeKnowledgeStore(tmp_path/'native').read_journal()) == 1


def test_personal_baseline_cannot_be_overwritten(tmp_path):
    g = gateway(tmp_path)
    c = g.connector_review.__self__
    original = update()
    c.knowledge_baseline = lambda: {'evidence':original['evidence'], 'claims':original['claims']}
    changed = update()
    changed['claims'][0]['statement'] = 'replacement'
    assert g.register_knowledge(changed)['reason'] == 'knowledge_replacement_requires_review'
    assert not c.native_knowledge.has_generations()


@pytest.mark.parametrize("input_format", ["object", "json-string"])
def test_http_json_registration_scopes_and_default_browser_path(tmp_path, signing_key, input_format):
    from starlette.testclient import TestClient
    from protocol.v1.tests.test_mcp_http import _config, _jwt, _jwks, _post, HEADERS
    from protocol.v1.tests.test_mcp_confirmed_writes import _write_config
    from protocol.v1.security.oauth_resource import JWTTokenVerifier
    from protocol.v1.transport.mcp_confirmed_writes import ConfirmedWriteSurface
    from protocol.v1.transport.mcp_http import create_http_app
    config = _write_config()
    grants = tuple(g.model_copy(update={'knowledge_registration':'append_only'}) for g in config.grants)
    config = config.model_copy(update={'grants': grants})
    resource = _config()
    backends = {}
    def factory(g):
        authority = _grant()
        principal = _principal()
        for obj in (authority, principal):
            obj['subject_user_id'],obj['client_id'] = g.subject,g.client_id
        result = gateway(tmp_path, grant=authority, principal=principal)
        backends[g.subject] = result
        return result
    surface = ConfirmedWriteSurface(resource, config, factory, audit_root=tmp_path/'audit')
    backends['fixture-reader'].portfolio_ids = ['portfolio-alpha','portfolio-beta']
    app = create_http_app(backends['fixture-reader'], JWTTokenVerifier(resource,_jwks(signing_key)),
                          writes=surface, write_input_format=input_format)
    headers = {**HEADERS,'Authorization':'Bearer '+_jwt(signing_key,scope='investkitchen.read investkitchen.write')}
    with TestClient(app, base_url='https://mcp.example.test') as client:
        tools = _post(client,'tools/list',headers=headers).json()['result']['tools']
        tool = next(t for t in tools if t['name']=='register_knowledge')
        assert tool['annotations']['readOnlyHint'] is False
        assert set(tool['inputSchema']['required']) == {'title','source_text','statements'}
        def call(name,args,h=headers):
            return _post(client,'tools/call',{'name':name,'arguments':args},headers=h).json()['result']
        if input_format == 'json-string':
            contract=call('get_write_contract',{'preview_tool':'register_knowledge'})['structuredContent']
            assert contract['approval']=='server_granted_append_only'
            assert contract['input_format']=='object'
        args={'update_json':json.dumps(update())} if input_format=='json-string' else {'update':update()}
        readonly={**headers,'Authorization':'Bearer '+_jwt(signing_key)}
        assert call('register_knowledge',args,readonly)['isError']
        result=call('register_knowledge',args)['structuredContent']
        assert result['status']=='applied' and result['read_back']['matches']
        assert not surface.pending  # no browser ticket or client confirmation
        from protocol.v1.tests.test_knowledge_note import NOTE
        note=call('register_knowledge',NOTE)['structuredContent']
        assert note['status']=='applied' and note['read_back']['matches']
        assert call('register_knowledge',NOTE)['structuredContent']['status']=='already_registered'
        assert call('register_knowledge',NOTE,readonly)['isError']
        changed=update('generation:review')
        changed['current_state']={'summary':'state change'}
        review=call('preview_knowledge_update',{'update_json':json.dumps(changed)} if input_format=='json-string' else {'update':changed})['structuredContent']
        assert review['approval_url']
        assert call('apply_knowledge_update',{k:review[k] for k in ('preview_id','confirmation')})['isError']

# Fixture imported explicitly: these are local integration tests, not real Gemini.
from protocol.v1.tests.test_mcp_http import signing_key


def test_addition_policy_schema_is_explicit_and_closed():
    from jsonschema import Draft202012Validator
    schema = json.loads((ROOT/'protocol/v1/schemas/remote-gateway.schema.json').read_text())
    policy = schema['$defs']['accessGrant']['properties']['request_policy']
    validator = Draft202012Validator(policy)
    valid = {'max_ttl_seconds':300,'max_future_skew_seconds':30,'knowledge_registration':'append_only'}
    validator.validate(valid)
    assert list(validator.iter_errors({**valid,'knowledge_registration':'all_writes'}))
