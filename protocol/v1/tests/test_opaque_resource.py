from __future__ import annotations

import asyncio
import base64
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest
from pydantic import ValidationError
from starlette.testclient import TestClient

from protocol.v1.security.opaque_resource import AutheliaTokenVerifier, IntrospectionConfig, load_introspection
from protocol.v1.tests.test_mcp_http import (
    HEADERS, ISSUER, READ_SCOPE, RESOURCE, _config, _gateway, _jwks, _jwt, _post, signing_key,
)
from protocol.v1.transport.mcp_http import create_http_app

TOKEN = 'authelia_at_' + 'synthetic-opaque-token.' + 'x' * 32


def profile(**kw):
    return IntrospectionConfig(endpoint='http://127.0.0.1:19091/api/oidc/introspection',
        client_id='gemini-fixture', client_secret='synthetic-secret', **kw)


def claims(**kw):
    now = int(time.time())
    return dict(active=True, iss=ISSUER, aud=[RESOURCE], sub='fixture-reader',
                client_id='gemini-fixture', scope=READ_SCOPE, iat=now-1, exp=now+299, **kw)


def verifier(key, monkeypatch, body=None):
    v = AutheliaTokenVerifier(_config(), _jwks(key), profile())
    monkeypatch.setattr(v, '_fetch', lambda token: claims() if body is None else body)
    return v


def test_opaque_and_prior_signed_tokens_use_same_grants(signing_key, monkeypatch):
    v = verifier(signing_key, monkeypatch)
    a = asyncio.run(v.verify_token(TOKEN))
    old = asyncio.run(v.verify_token(_jwt(signing_key)))
    assert a.subject == old.subject == 'fixture-reader'
    assert a.resource == RESOURCE and a.client_id == old.client_id
    assert a.scopes == old.scopes == [READ_SCOPE]


@pytest.mark.parametrize('changes', [
    {'active': False}, {'active': 1}, {'active': 'true'}, {'iss': 'https://wrong.test'},
    {'aud': ['https://wrong.test/mcp']}, {'aud': {'resource': RESOURCE}}, {'aud': [RESOURCE, None]},
    {'sub': 'ungranted'}, {'sub': None}, {'client_id': 'another-client'}, {'client_id': None},
    {'iat': True}, {'exp': True}, {'exp': 1}, {'exp': '9999999999'},
    {'iat': int(time.time()) + 3600}, {'nbf': int(time.time()) + 3600}, {'nbf': True},
    {'scope': [READ_SCOPE]}, {'scope': None},
])
def test_invalid_introspection_fails_closed(signing_key, monkeypatch, changes):
    c = claims(); c.update(changes)
    assert asyncio.run(verifier(signing_key, monkeypatch, c).verify_token(TOKEN)) is None


@pytest.mark.parametrize('missing', ['active', 'iss', 'aud', 'sub', 'client_id', 'iat', 'exp', 'scope'])
def test_required_introspection_claims(signing_key, monkeypatch, missing):
    c = claims(); del c[missing]
    assert asyncio.run(verifier(signing_key, monkeypatch, c).verify_token(TOKEN)) is None


def test_outage_never_falls_back_to_token_claims_or_cached_auth(signing_key, monkeypatch):
    v = verifier(signing_key, monkeypatch)
    assert asyncio.run(v.verify_token(TOKEN)) is not None
    def failed(token): raise TimeoutError('sensitive-provider-body')
    monkeypatch.setattr(v, '_fetch', failed)
    assert asyncio.run(v.verification_result_async(TOKEN)) == (None, 'introspection_unavailable')
    assert asyncio.run(v.verify_token(_jwt(signing_key))) is not None


@pytest.mark.parametrize('token', ['authelia_at_short', 'authelia_at_'+'x'*17000,
                                  TOKEN+'\n', 'authelia_rt_'+'x'*64, 'unsigned-token'])
def test_other_or_malformed_tokens_do_not_call_introspection(signing_key, monkeypatch, token):
    v = verifier(signing_key, monkeypatch)
    monkeypatch.setattr(v, '_fetch', lambda token: pytest.fail('Unexpected introspection'))
    assert asyncio.run(v.verify_token(token)) is None


@pytest.mark.parametrize('endpoint', [
    'https://auth.example.test/api/oidc/introspection',
    'http://localhost:19091/api/oidc/introspection',
    'http://192.0.2.1:19091/api/oidc/introspection',
    'http://127.0.0.1:19091/another-path',
    'http://user@127.0.0.1:19091/api/oidc/introspection',
    'http://127.0.0.1:19091/api/oidc/introspection?redirect=x',
    'http://127.0.0.1/api/oidc/introspection',
])
def test_introspection_endpoint_is_exact_and_loopback(endpoint):
    with pytest.raises(ValidationError):
        IntrospectionConfig(endpoint=endpoint, client_id='fixture', client_secret='synthetic')


def test_private_config_and_secret_representation(tmp_path):
    p = tmp_path/'introspection.json'
    p.write_text(json.dumps({'endpoint':'http://127.0.0.1:19091/api/oidc/introspection',
        'client_id':'fixture','client_secret':'synthetic-not-for-output'}))
    p.chmod(0o600)
    assert 'synthetic-not-for-output' not in repr(load_introspection(p))
    p.chmod(0o644)
    with pytest.raises(ValueError): load_introspection(p)
    p.chmod(0o600)
    link=tmp_path/'link';link.symlink_to(p)
    with pytest.raises(ValueError): load_introspection(link)


def test_http_reads_remain_scoped_and_revocation_is_immediate(signing_key, monkeypatch):
    c=claims();v=verifier(signing_key,monkeypatch,c)
    with TestClient(create_http_app(_gateway(),v),base_url='https://mcp.example.test') as client:
        h={**HEADERS,'Authorization':'Bearer '+TOKEN}
        tools=_post(client,'tools/list',headers=h).json()['result']['tools']
        assert any(t['name']=='get_portfolio_state' for t in tools)
        assert not any(t['name'].startswith(('apply_','preview_')) for t in tools)
        own=_post(client,'tools/call',{'name':'get_portfolio_state','arguments':{'portfolio_id':'portfolio-alpha'}},headers=h)
        assert own.json()['result'].get('isError') is not True
        other=_post(client,'tools/call',{'name':'get_portfolio_state','arguments':{'portfolio_id':'portfolio-beta'}},headers=h)
        assert other.json()['result']['isError'] is True
        c['active']=False
        assert _post(client,'tools/list',headers=h).status_code==401


def test_bounded_concurrency_fails_closed(signing_key,monkeypatch):
    v=verifier(signing_key,monkeypatch)
    v._slots.acquire();v._slots.acquire()
    try:
        assert asyncio.run(v.verification_result_async(TOKEN))==(None,'introspection_unavailable')
    finally:v._slots.release();v._slots.release()


@pytest.mark.parametrize('mode', ['valid','redirect','non_json','oversized','bad_json','unauthorized'])
def test_real_loopback_http_contract(signing_key,mode):
    seen=[]
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def do_POST(self):
            assert self.headers['Host'] == 'auth.example.test'
            assert self.headers['X-Forwarded-Proto'] == 'https'
            seen.append((self.path,self.headers.get('Authorization'),parse_qs(self.rfile.read(int(self.headers['Content-Length'])).decode())))
            status=302 if mode=='redirect' else 401 if mode=='unauthorized' else 200
            self.send_response(status)
            if mode=='redirect':self.send_header('Location','http://127.0.0.1:9/never-follow')
            self.send_header('Content-Type','text/html' if mode=='non_json' else 'application/json')
            self.end_headers()
            body=b'x'*65537 if mode=='oversized' else b'invalid' if mode=='bad_json' else json.dumps(claims()).encode()
            self.wfile.write(body)
    server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
    worker=threading.Thread(target=server.serve_forever,daemon=True);worker.start()
    try:
        cfg=profile().model_copy(update={'endpoint':f'http://127.0.0.1:{server.server_port}/api/oidc/introspection'})
        v=AutheliaTokenVerifier(_config(),_jwks(signing_key),cfg)
        result=asyncio.run(v.verify_token(TOKEN))
        assert (result is not None)==(mode=='valid')
        assert len(seen)==1 and seen[0][0]=='/api/oidc/introspection'
        assert seen[0][1]=='Basic '+base64.b64encode(b'gemini-fixture:synthetic-secret').decode()
        assert seen[0][2]=={'token':[TOKEN],'token_type_hint':['access_token']}
    finally:server.shutdown();server.server_close();worker.join()


def test_opaque_write_still_requires_exact_browser_approval(setup, signing_key, monkeypatch):
    from protocol.v1.tests.test_mcp_confirmed_writes import _preview, _call, _approve
    _, _, writes, _, backends = setup
    c = claims()
    c['scope'] += ' investkitchen.write'
    v = verifier(signing_key, monkeypatch, c)
    with TestClient(create_http_app(backends['fixture-reader'], v, writes=writes),
                    base_url='https://mcp.example.test') as client:
        h = {**HEADERS, 'Authorization': 'Bearer ' + TOKEN}
        preview = _preview(client, h)
        args = {k: preview[k] for k in ('preview_id', 'confirmation')}
        assert _call(client, h, 'apply_knowledge_update', args)['isError']
        _approve(client, preview)
        result = _call(client, h, 'apply_knowledge_update', args)['structuredContent']
        assert result['status'] == 'applied'
        assert result['read_back']['matches'] is True


from protocol.v1.tests.test_mcp_confirmed_writes import setup  # shared synthetic store fixture

@pytest.mark.parametrize('diagnostic_enabled', [False, True])
def test_transient_auth_outage_is_503_without_challenge_then_recovers(signing_key, monkeypatch, tmp_path, diagnostic_enabled):
    from protocol.v1.transport.http_diagnostics import private_transport_diagnostics
    v=verifier(signing_key,monkeypatch)
    (tmp_path/'diagnostics').mkdir(mode=0o700)
    diagnostics=private_transport_diagnostics(tmp_path/'diagnostics', [], set()) if diagnostic_enabled else None
    app=create_http_app(_gateway(),v,diagnostics=diagnostics)
    with TestClient(app,base_url='https://mcp.example.test') as client:
        h={**HEADERS,'Authorization':'Bearer '+TOKEN}
        def fail(token): raise TimeoutError('provider secret must never escape')
        monkeypatch.setattr(v,'_fetch',fail)
        response=_post(client,'tools/list',headers=h)
        assert response.status_code==503
        assert 'WWW-Authenticate' not in response.headers
        assert response.headers['Retry-After']=='5'
        assert response.headers['Cache-Control']=='no-store'
        assert response.json()=={'error':'authentication_temporarily_unavailable'}
        monkeypatch.setattr(v,'_fetch',lambda token:claims())
        assert _post(client,'tools/list',headers=h).status_code==200
        monkeypatch.setattr(v,'_fetch',lambda token:{'active':False})
        assert _post(client,'tools/list',headers=h).status_code==401
        assert _post(client,'tools/list',headers=HEADERS).status_code==401
