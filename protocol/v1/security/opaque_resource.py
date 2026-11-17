"""Opt-in Authelia opaque access tokens with authenticated loopback introspection.

No tokens are issued or persisted here. JWT verification remains available for
unexpired pre-cutover tokens; opaque tokens never inherit permissions from an
introspection result. The existing operator-owned subject/client grants apply.
"""
from __future__ import annotations

import base64
import ipaddress
import json
import stat
import threading
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlencode, urlsplit

import anyio
from mcp.server.auth.provider import AccessToken
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from protocol.v1.security.oauth_resource import JWTTokenVerifier


class IntrospectionConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    endpoint: str
    client_id: str = Field(min_length=1)
    client_secret: SecretStr
    timeout_seconds: float = Field(default=5, ge=0.1, le=10)

    @field_validator("endpoint")
    @classmethod
    def loopback_only(cls, value):
        u = urlsplit(value)
        if (u.scheme != "http" or not u.hostname or u.username or u.password
                or u.query or u.fragment or u.path != "/api/oidc/introspection"
                or not ipaddress.ip_address(u.hostname).is_loopback or not u.port):
            raise ValueError("Introspection requires an exact loopback Authelia endpoint")
        return value

    @field_validator("client_id")
    @classmethod
    def basic_identifier(cls, value):
        if ":" in value or any(ord(c) < 32 or ord(c) > 126 for c in value):
            raise ValueError("Invalid introspection client identifier")
        return value

    @field_validator("client_secret")
    @classmethod
    def nonempty_secret(cls, value):
        if not value.get_secret_value():
            raise ValueError("Introspection client credential required")
        return value


def load_introspection(path: Path) -> IntrospectionConfig:
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
        raise ValueError("Introspection configuration must be a private regular file")
    return IntrospectionConfig.model_validate_json(path.read_text(encoding="utf-8"))


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class AutheliaTokenVerifier(JWTTokenVerifier):
    def __init__(self, config, jwks, introspection: IntrospectionConfig):
        super().__init__(config, jwks)
        if introspection.client_id not in {g.client_id for g in config.grants}:
            raise ValueError("Introspection client must have an existing read grant")
        self.introspection = introspection
        self._slots = threading.BoundedSemaphore(2)
        # Bound queued work as well as issuer concurrency. A normal MCP client
        # can initialize parallel calls while its GET/notification is in flight.
        self._admissions = threading.BoundedSemaphore(8)

    async def verify_token(self, token: str) -> AccessToken | None:
        return (await self.verification_result_async(token))[0]

    async def verification_result_async(self, token: str):
        if not token.startswith("authelia_at_"):
            return super().verification_result(token)
        if not 32 <= len(token) <= 16384 or any(ord(c) < 33 or ord(c) > 126 for c in token):
            return None, "malformed"
        if not self._admissions.acquire(blocking=False):
            return None, "introspection_unavailable"
        try:
            return await anyio.to_thread.run_sync(self._queued_opaque_result, token)
        finally:
            self._admissions.release()

    def _queued_opaque_result(self, token):
        # Wait off the event loop; never cache a prior authorization decision.
        if not self._slots.acquire(timeout=self.introspection.timeout_seconds):
            return None, "introspection_unavailable"
        try:
            return self._opaque_result(token)
        finally:
            self._slots.release()

    def _fetch(self, token):
        c = self.introspection
        basic = base64.b64encode((c.client_id + ":" + c.client_secret.get_secret_value()).encode()).decode()
        request = urllib.request.Request(c.endpoint,
            data=urlencode({"token": token, "token_type_hint": "access_token"}).encode(),
            headers={"Authorization": "Basic " + basic, "Content-Type": "application/x-www-form-urlencoded",
                     "Accept": "application/json", "Host": urlsplit(self.config.issuer_url).netloc,
                     "X-Forwarded-Proto": "https"})
        # Neither credentials nor tokens may follow a redirect or ambient proxy.
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=c.timeout_seconds) as response:
            if response.status != 200 or response.headers.get_content_type() != "application/json":
                raise ValueError("Invalid introspection response")
            body = response.read(65537)
            if len(body) > 65536:
                raise ValueError("Oversized introspection response")
            return json.loads(body)

    def _opaque_result(self, token):
        try:
            claims = self._fetch(token)
            if not isinstance(claims, dict) or claims.get("active") is not True:
                return None, "inactive"
            now = time.time()
            if claims.get("iss") != self.config.issuer_url:
                return None, "wrong_issuer"
            aud = claims.get("aud")
            if not (aud == self.config.resource_url or isinstance(aud, list)
                    and all(isinstance(a, str) for a in aud) and self.config.resource_url in aud):
                return None, "wrong_audience"
            subject, client = claims.get("sub"), claims.get("client_id")
            if (not isinstance(subject, str) or not isinstance(client, str)
                    or client != self.introspection.client_id or self.config.grant_for(subject, client) is None
                    or any(type(claims.get(f)) is not int for f in ("iat", "exp"))):
                return None, "invalid_identity_or_time"
            if claims["exp"] <= now:
                return None, "expired"
            if claims["iat"] > now or claims["exp"] <= claims["iat"]:
                return None, "invalid_identity_or_time"
            if "nbf" in claims and (type(claims["nbf"]) is not int or claims["nbf"] > now):
                return None, "not_yet_valid"
            # RFC 7662 uses a space-delimited scope even when JWTs use scp arrays.
            scope = claims.get("scope")
            if not isinstance(scope, str):
                return None, "invalid_scope_claim"
            return AccessToken(token=token, subject=subject, client_id=client,
                scopes=scope.split(), expires_at=claims["exp"], resource=self.config.resource_url,
                claims={"iss": claims["iss"]}), "accepted"
        except Exception:
            # No provider body, exception text, URL, token, or credential leaves this boundary.
            return None, "introspection_unavailable"
