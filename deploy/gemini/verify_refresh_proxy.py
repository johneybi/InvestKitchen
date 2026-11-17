"""Exercise duplicate refresh against an isolated real Authelia, synthetic data only."""
from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import secrets
import socket
import subprocess
import tempfile
import time
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlencode, urlsplit, parse_qs

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

try:
    from deploy.gemini.oauth_refresh_proxy import LoopbackIssuer, RefreshGate
except ModuleNotFoundError:
    from oauth_refresh_proxy import LoopbackIssuer, RefreshGate


def run(binary):
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix='investkitchen-refresh-synthetic-') as directory:
        root = Path(directory)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        generated = subprocess.run([str(binary), 'crypto', 'hash', 'generate', 'argon2', '--random',
                                    '--random.length', '48', '--no-confirm'], capture_output=True, text=True, check=True)
        fields = {line.split(':', 1)[0].strip(): line.split(':', 1)[1].strip()
                  for line in generated.stdout.splitlines() if ':' in line}
        (root / 'users.json').write_text(json.dumps({'users': {'synthetic-owner': {
            'displayname': 'Synthetic owner', 'password': fields['Digest'],
            'email': 'synthetic@example.invalid', 'groups': []}}}))
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption()).decode()
        secret = secrets.token_urlsafe(32)
        origin = 'https://mcp.example.test'
        redirect = 'http://127.0.0.1:8099/callback'
        config = {
            'server': {'address': f'tcp://127.0.0.1:{port}/'},
            'log': {'level': 'warn', 'format': 'json'},
            'totp': {'disable': True}, 'webauthn': {'disable': True}, 'duo_api': {'disable': True},
            'authentication_backend': {'password_reset': {'disable': True},
                'file': {'path': str(root / 'users.json'), 'watch': False}},
            'access_control': {'default_policy': 'deny', 'rules': [{'domain': 'mcp.example.test', 'policy': 'one_factor'}]},
            'session': {'secret': secrets.token_urlsafe(48), 'cookies': [
                {'domain': 'mcp.example.test', 'authelia_url': origin}]},
            'storage': {'encryption_key': secrets.token_urlsafe(48), 'local': {'path': str(root / 'db.sqlite3')}},
            'notifier': {'filesystem': {'filename': str(root / 'notifications.txt')}},
            'identity_providers': {'oidc': {
                'hmac_secret': secrets.token_urlsafe(48),
                'jwks': [{'key_id': 'synthetic', 'algorithm': 'RS256', 'use': 'sig', 'key': pem}],
                'lifespans': {'access_token': '5 minutes', 'refresh_token': '10 minutes'},
                'clients': [{'client_id': 'synthetic-client', 'client_secret': '$plaintext$' + secret,
                             'authorization_policy': 'one_factor', 'consent_mode': 'implicit',
                             'redirect_uris': [redirect], 'scopes': ['openid', 'offline_access'],
                             'grant_types': ['authorization_code', 'refresh_token'],
                             'response_types': ['code'], 'token_endpoint_auth_method': 'client_secret_post'}]}}}
        config_path = root / 'configuration.json'
        config_path.write_text(json.dumps(config))
        validated = subprocess.run([str(binary), 'validate-config', '--config', str(config_path)], capture_output=True)
        if validated.returncode:
            # Only configuration error categories, no generated values.
            raise RuntimeError('synthetic configuration rejected')
        with (root / 'service.log').open('wb') as log:
            process = subprocess.Popen([str(binary), '--config', str(config_path)], stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 30
                while True:
                    if process.poll() is not None:
                        raise RuntimeError('synthetic issuer exited')
                    try:
                        with socket.create_connection(('127.0.0.1', port), timeout=.2): break
                    except OSError:
                        if time.monotonic() >= deadline: raise RuntimeError('synthetic issuer startup timeout')
                        time.sleep(.1)
                cookie = ''
                def request(method, path, value=None):
                    nonlocal cookie
                    conn = http.client.HTTPConnection('127.0.0.1', port, timeout=60)
                    headers = {'Host': 'mcp.example.test', 'X-Forwarded-Proto': 'https',
                               'X-Forwarded-Host': 'mcp.example.test', 'X-Forwarded-For': '127.0.0.1',
                               'Origin': origin, 'Content-Type': 'application/json', 'Cookie': cookie}
                    try:
                        conn.request(method, path, json.dumps(value) if value else None, headers)
                        response = conn.getresponse()
                        jar = SimpleCookie()
                        for k,v in response.getheaders():
                            if k.lower() == 'set-cookie': jar.load(v)
                        if 'authelia_session' in jar:
                            cookie = 'authelia_session=' + jar['authelia_session'].value
                        return response.status, dict(response.getheaders()), response.read()
                    finally: conn.close()
                status, _, _ = request('POST', '/api/firstfactor', {'username': 'synthetic-owner',
                    'password': fields['Random Password'], 'keepMeLoggedIn': False,
                    'targetURL': origin, 'requestMethod': 'GET'})
                if status != 200: raise RuntimeError('synthetic login failed')
                print('SYNTHETIC: issuer ready and test account authenticated', flush=True)
                verifier = secrets.token_urlsafe(48)
                challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
                query = urlencode({'client_id': 'synthetic-client', 'redirect_uri': redirect,
                                   'response_type': 'code', 'scope': 'openid offline_access',
                                   'state': secrets.token_urlsafe(16), 'nonce': secrets.token_urlsafe(16),
                                   'code_challenge': challenge, 'code_challenge_method': 'S256'})
                status, headers, _ = request('GET', '/api/oidc/authorization?' + query)
                location = headers.get('Location', headers.get('location', ''))
                code = parse_qs(urlsplit(location).query).get('code', [None])[0]
                flow = parse_qs(urlsplit(location).query).get('flow_id', [None])[0]
                if not code and flow:
                    status, _, raw = request('GET', '/api/oidc/consent?' + urlencode({'flow_id': flow}))
                    info = json.loads(raw).get('data', {})
                    assert status == 200 and info.get('client_id') == 'synthetic-client'
                    assert set(info.get('scopes', [])) == {'openid', 'offline_access'}
                    status, _, raw = request('POST', '/api/oidc/consent', {
                        'flow_id': flow, 'client_id': 'synthetic-client', 'consent': True,
                        'pre_configure': False, 'claims': []})
                    target = json.loads(raw).get('data', {}).get('redirect_uri', '')
                    if urlsplit(target).netloc == 'mcp.example.test':
                        parsed = urlsplit(target)
                        status, headers, _ = request('GET', parsed.path + '?' + parsed.query)
                        target = headers.get('Location', headers.get('location', ''))
                    assert target.startswith(redirect + '?')
                    code = parse_qs(urlsplit(target).query).get('code', [None])[0]
                if not code: raise RuntimeError('synthetic authorization code unavailable: ' + json.dumps({'status': status, 'path': urlsplit(location).path, 'query_keys': list(parse_qs(urlsplit(location).query)), 'error': parse_qs(urlsplit(location).query).get('error'), 'description': parse_qs(urlsplit(location).query).get('error_description')}))
                print('SYNTHETIC: authorization code obtained', flush=True)
                issuer = LoopbackIssuer(port, origin)
                initial = issuer.exchange(urlencode({'grant_type': 'authorization_code', 'client_id': 'synthetic-client',
                    'client_secret': secret, 'code': code, 'redirect_uri': redirect, 'code_verifier': verifier}).encode())
                if initial.status != 200: raise RuntimeError('synthetic code exchange failed')
                original = json.loads(initial.body)
                refresh = urlencode({'grant_type': 'refresh_token', 'client_id': 'synthetic-client',
                    'client_secret': secret, 'refresh_token': original['refresh_token']}).encode()
                gate = RefreshGate(issuer, 'synthetic-client')
                first = gate.handle(refresh)
                duplicate = gate.handle(refresh)
                if first.status != 200 or duplicate.status != 200:
                    raise RuntimeError('duplicate suppression or introspection failed')
                a, b = json.loads(first.body), json.loads(duplicate.body)
                assert a['access_token'] == b['access_token'] and a['refresh_token'] == b['refresh_token']
                # A second, genuine rotation still reaches the issuer.
                second_request = urlencode({'grant_type': 'refresh_token', 'client_id': 'synthetic-client',
                    'client_secret': secret, 'refresh_token': a['refresh_token']}).encode()
                second = gate.handle(second_request)
                assert second.status == 200
                c = json.loads(second.body)
                assert c['refresh_token'] != a['refresh_token']
                # Real issuer rejects consumed tokens without the retry gate.
                assert issuer.exchange(refresh).status == 400
                assert not issuer.introspect(c['access_token'], 'synthetic-client', secret)['active']
                assert gate.handle(second_request).status == 400
                print('REAL SYNTHETIC ISSUER PASS: duplicate response, successive rotation, reuse revocation, live cache rejection')
            finally:
                process.terminate()
                try: process.wait(timeout=5)
                except subprocess.TimeoutExpired: process.kill(); process.wait(timeout=5)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--authelia', type=Path, required=True)
    args = parser.parse_args()
    run(args.authelia.resolve())
