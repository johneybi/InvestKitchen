"""OAuth resource-server verification; authorization grants are operator-owned.

An external authorization server supplies login, consent, PKCE and tokens.
Only public RSA verification keys are loaded here. No token is issued by MCP.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import jwt
from mcp.server.auth.provider import AccessToken
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


READ_SCOPE = "investkitchen.read"
ReadPermission = Literal[
    "system.capabilities.read", "portfolio.read", "knowledge.read",
    "market.read", "decision.context.build",
]


class ReadGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    permissions: tuple[ReadPermission, ...] = Field(min_length=1)
    portfolio_scope: tuple[str, ...] = ()

    @field_validator("portfolio_scope")
    @classmethod
    def valid_scope(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        if any(not value.strip() for value in values) or len(set(values)) != len(values):
            raise ValueError("Invalid portfolio scope")
        return values


class OAuthResourceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    issuer_url: str
    resource_url: str
    client_id_claim: str = Field(default="client_id", min_length=1)
    grants: tuple[ReadGrant, ...] = Field(min_length=1)

    @field_validator("issuer_url", "resource_url")
    @classmethod
    def https_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("OAuth URLs must be absolute HTTPS URLs")
        # Accessing port also rejects malformed/out-of-range port values.
        _ = parsed.port
        return value

    @model_validator(mode="after")
    def unique_grants(self) -> OAuthResourceConfig:
        if urlsplit(self.resource_url).path != "/mcp":
            raise ValueError("Resource URL must end in /mcp")
        identities = [(grant.subject, grant.client_id) for grant in self.grants]
        if len(set(identities)) != len(identities):
            raise ValueError("Duplicate subject/client grant")
        if self.client_id_claim in {"sub", "iss", "aud", "scope", "exp", "iat", "nbf"}:
            raise ValueError("Client identity must have its own claim")
        return self

    def grant_for(self, subject: str | None, client_id: str) -> ReadGrant | None:
        return next((grant for grant in self.grants
                     if grant.subject == subject and grant.client_id == client_id), None)


class JWTTokenVerifier:
    """Verify RS256 JWTs against an explicit local, public-only JWKS snapshot."""

    def __init__(self, config: OAuthResourceConfig, jwks: dict) -> None:
        self.config = config
        keys = jwks.get("keys") if isinstance(jwks, dict) else None
        if not isinstance(keys, list) or not keys:
            raise ValueError("Public verification keys are required")
        self.keys: dict[str, jwt.PyJWK] = {}
        for raw in keys:
            if (not isinstance(raw, dict) or raw.get("kty") != "RSA"
                    or raw.get("alg", "RS256") != "RS256"
                    or raw.get("use", "sig") != "sig"
                    or raw.get("key_ops", ["verify"]) != ["verify"]
                    or set(raw) & {"d", "p", "q", "dp", "dq", "qi", "oth", "k"}):
                raise ValueError("Only public RS256 verification keys are accepted")
            kid = raw.get("kid")
            if not isinstance(kid, str) or not kid or kid in self.keys:
                raise ValueError("Verification key IDs must be unique")
            try:
                key = jwt.PyJWK.from_dict(raw, algorithm="RS256")
            except (jwt.PyJWTError, TypeError, ValueError):
                raise ValueError("Invalid public verification key") from None
            if key.key.key_size < 2048:
                raise ValueError("RSA keys must have at least 2048 bits")
            self.keys[kid] = key

    async def verify_token(self, token: str) -> AccessToken | None:
        if len(token) > 16384:
            return None
        try:
            header = jwt.get_unverified_header(token)
            if header.get("alg") != "RS256" or header.get("crit"):
                return None
            key = self.keys.get(header.get("kid"))
            if key is None:
                return None
            claims = jwt.decode(
                token, key.key, algorithms=["RS256"],
                issuer=self.config.issuer_url, audience=self.config.resource_url,
                options={"require": ["iss", "aud", "exp", "iat", "sub", self.config.client_id_claim]},
            )
            subject = claims["sub"]
            client_id = claims[self.config.client_id_claim]
            scope = claims.get("scope", "")
            if (not isinstance(subject, str) or not isinstance(client_id, str)
                    or not isinstance(scope, str)
                    or any(not isinstance(claims[field], int) or isinstance(claims[field], bool)
                           for field in ("iat", "exp"))
                    or self.config.grant_for(subject, client_id) is None):
                return None
            return AccessToken(
                token=token, subject=subject, client_id=client_id,
                scopes=scope.split(), expires_at=claims["exp"],
                resource=self.config.resource_url, claims={"iss": claims["iss"]},
            )
        except (jwt.PyJWTError, ValueError, TypeError):
            return None


def load_oauth_resource(config_path: Path, jwks_path: Path) -> JWTTokenVerifier:
    config = OAuthResourceConfig.model_validate_json(config_path.read_text(encoding="utf-8"))
    return JWTTokenVerifier(config, json.loads(jwks_path.read_text(encoding="utf-8")))
