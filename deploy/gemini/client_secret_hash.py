"""Rehash one existing OIDC secret in memory; no credential rotation or file I/O."""
import base64
import copy
import hashlib
import secrets


def pbkdf2_digest(secret):
    """Authelia's documented OIDC default; never use this for user passwords."""
    salt = secrets.token_bytes(32)
    derived = hashlib.pbkdf2_hmac('sha512', secret.encode(), salt, 310000)
    def encode(value):
        return base64.b64encode(value).decode().rstrip('=').replace('+', '.')
    return '$pbkdf2-sha512$310000$' + encode(salt) + '$' + encode(derived)


def rehash_client(config, client_id, secret):
    from cryptography.hazmat.primitives.kdf.argon2 import Argon2id
    if not isinstance(secret, str) or len(secret) < 32:
        raise ValueError('existing machine secret is unsuitable')
    candidate = copy.deepcopy(config)
    clients = candidate['identity_providers']['oidc']['clients']
    matches = [c for c in clients if c.get('client_id') == client_id]
    if len(matches) != 1:
        raise ValueError('expected exactly one existing client')
    client = matches[0]
    original = client.get('client_secret', '')
    # This narrowly scoped migration only accepts the reviewed deployed profile.
    if not original.startswith('$argon2id$v=19$m=65536,t=3,p=4$'):
        raise ValueError('unexpected existing hash profile')
    try:
        Argon2id.verify_phc_encoded(secret.encode(), original)
    except Exception:
        raise ValueError('existing client secret verification failed') from None
    client['client_secret'] = pbkdf2_digest(secret)
    return candidate
