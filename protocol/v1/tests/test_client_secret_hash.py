import copy
import hashlib
import base64
import pytest
from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
from deploy.gemini.client_secret_hash import rehash_client


@pytest.fixture
def original():
    secret = 'synthetic-only-machine-credential-' * 2
    digest = Argon2id(salt=b'synthetic-salt-32-bytes-long!!!!!', length=32,
                     iterations=3, lanes=4, memory_cost=65536).derive_phc_encoded(secret.encode())
    return {'identity_providers': {'oidc': {'clients': [
        {'client_id': 'synthetic', 'client_secret': digest, 'scopes': ['openid'], 'lifespan': 'same'},
        {'client_id': 'other', 'client_secret': 'untouched'}]}},
        'authentication_backend': {'file': {'path': 'unchanged'}}}, secret


def test_same_secret_only_target_digest_changes(original):
    config, secret = original
    before = copy.deepcopy(config)
    candidate = rehash_client(config, 'synthetic', secret)
    digest = candidate['identity_providers']['oidc']['clients'][0]['client_secret']
    _, algorithm, count, salt, derived = digest.split('$')
    def decode(value):
        return base64.b64decode(value.replace('.', '+')+'='*(-len(value)%4))
    assert algorithm == 'pbkdf2-sha512' and count == '310000'
    assert hashlib.pbkdf2_hmac('sha512', secret.encode(), decode(salt), int(count)) == decode(derived)
    candidate['identity_providers']['oidc']['clients'][0]['client_secret'] = before['identity_providers']['oidc']['clients'][0]['client_secret']
    assert candidate == config == before


def test_wrong_secret_fails_closed(original):
    config, _ = original
    with pytest.raises(ValueError, match='verification failed'):
        rehash_client(config, 'synthetic', 'wrong-synthetic-secret-'*3)


def test_missing_and_duplicate_clients_rejected(original):
    config, secret = original
    with pytest.raises(ValueError): rehash_client(config, 'missing', secret)
    config['identity_providers']['oidc']['clients'].append(copy.deepcopy(config['identity_providers']['oidc']['clients'][0]))
    with pytest.raises(ValueError): rehash_client(config, 'synthetic', secret)


def test_unreviewed_hash_profile_rejected(original):
    config, secret = original
    config['identity_providers']['oidc']['clients'][0]['client_secret'] = '$plaintext$' + secret
    with pytest.raises(ValueError): rehash_client(config, 'synthetic', secret)
