"""Exercise real TLS, PKCE and MCP using only a generated synthetic trial."""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import secrets
import ssl
import sys
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2 as httpx


class LoginForm(HTMLParser):
    def __init__(self):
        super().__init__()
        self.action = None
        self.fields = {}

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "form" and attrs.get("id") == "kc-form-login":
            self.action = attrs.get("action")
        if tag == "input" and attrs.get("type") == "hidden" and attrs.get("name"):
            self.fields[attrs["name"]] = attrs.get("value", "")


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def verify(root: Path, ca: Path | None, *, oauth_only: bool = False) -> None:
    config = json.loads((root / "oauth/oauth-resource.json").read_text())
    realm = json.loads((root / "auth/import/investkitchen-realm.json").read_text())
    credentials = json.loads((root / "credentials.json").read_text())
    require(realm["users"][0]["username"] == "synthetic-owner", "Synthetic owner required")
    require(config["grants"][0]["portfolio_scope"] == ["portfolio-alpha"], "Synthetic grant required")
    require((root / "synthetic/personal/portfolio.json").is_file(), "Synthetic bundle required")
    issuer, resource = config["issuer_url"], config["resource_url"]
    origin = resource.removesuffix("/mcp")
    context = ssl.create_default_context(cafile=str(ca) if ca else None)
    with httpx.Client(verify=context, timeout=20, follow_redirects=False, trust_env=False) as client:
        metadata = client.get(issuer + "/.well-known/openid-configuration")
        require(metadata.status_code == 200, "Authorization discovery failed")
        metadata = metadata.json()
        require(metadata["issuer"] == issuer, "Issuer discovery mismatch")
        for key in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            require(metadata[key].startswith(issuer + "/protocol/openid-connect/"), "Unexpected authorization endpoint")
        require(client.get(origin + "/admin/").status_code == 404, "Admin route must be blocked")
        require(client.get(origin + "/realms/master/.well-known/openid-configuration").status_code == 404,
                "Master realm must be blocked")
        jwks = client.get(metadata["jwks_uri"])
        require(jwks.status_code == 200, "Public signing keys unavailable")
        from protocol.v1.security.oauth_resource import JWTTokenVerifier, OAuthResourceConfig
        signing_jwks = {"keys": [key for key in jwks.json()["keys"]
                                if key.get("kty") == "RSA" and key.get("use") == "sig" and key.get("alg") == "RS256"]}
        verifier = JWTTokenVerifier(OAuthResourceConfig.model_validate(config), signing_jwks)
        (root / "oauth/public-jwks.json").write_text(json.dumps(signing_jwks) + "\n")
        (root / "oauth/public-jwks.json").chmod(0o600)
        callback = realm["clients"][0]["redirectUris"][0]
        state, pkce_verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(pkce_verifier.encode()).digest()).rstrip(b"=").decode()
        params = {"client_id": credentials["client_id"], "response_type": "code", "redirect_uri": callback,
                  "scope": "investkitchen.read", "state": state, "code_challenge": challenge,
                  "code_challenge_method": "S256", "resource": resource}
        denied = client.get(metadata["authorization_endpoint"], params={**params, "redirect_uri": "https://untrusted.example.invalid/callback"})
        require(denied.status_code == 400, "Unregistered callback was not rejected")
        login = client.get(metadata["authorization_endpoint"], params=params)
        require(login.status_code == 200, "PKCE login page unavailable")
        form = LoginForm()
        form.feed(login.text)
        require(form.action and form.action.startswith(issuer + "/login-actions/"), "Unexpected login form destination")
        logged_in = client.post(form.action, data={**form.fields, "username": credentials["username"],
                                                 "password": credentials["password"], "credentialId": ""})
        require(logged_in.status_code in (302, 303), "Synthetic login did not complete")
        location = urlsplit(logged_in.headers["location"])
        expected = urlsplit(callback)
        require((location.scheme, location.netloc, location.path) == (expected.scheme, expected.netloc, expected.path),
                "Authorization callback mismatch")
        query = parse_qs(location.query)
        require(query.get("state") == [state] and "code" in query, "Authorization code or state unavailable")
        payload = {"grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": callback,
                   "client_id": credentials["client_id"], "client_secret": credentials["client_secret"],
                   "code_verifier": pkce_verifier, "resource": resource}
        no_pkce = client.post(metadata["token_endpoint"], data={k: v for k, v in payload.items() if k != "code_verifier"})
        require(no_pkce.status_code == 400, "Missing PKCE verifier must be rejected")
        # A failed token exchange can consume the code. Acquire a new code using
        # the authenticated session, without repeating a password login.
        authorized = client.get(metadata["authorization_endpoint"], params=params)
        require(authorized.status_code in (302, 303), "Authorization session unavailable")
        query = parse_qs(urlsplit(authorized.headers["location"]).query)
        require(query.get("state") == [state] and "code" in query, "Authorization state mismatch")
        payload["code"] = query["code"][0]
        token_response = client.post(metadata["token_endpoint"], data=payload)
        require(token_response.status_code == 200, "Authorization code exchange failed")
        token = token_response.json()["access_token"]
        import asyncio
        require(asyncio.run(verifier.verify_token(token)) is not None, "Real signed token failed grant verification")
        print("TLS, discovery, exact callback, PKCE and signed read grant: PASS")
        if oauth_only:
            return
        denied = client.post(resource, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        require(denied.status_code == 401, "Unauthenticated MCP request must fail")
        headers = {"Accept": "application/json, text/event-stream", "Authorization": "Bearer " + token}
        def rpc(method, params=None, request_id=1):
            body = {"jsonrpc": "2.0", "id": request_id, "method": method}
            if params is not None:
                body["params"] = params
            response = client.post(resource, headers=headers, json=body)
            require(response.status_code == 200, "Authenticated MCP request failed")
            directives = {part.strip().lower() for part in response.headers.get("cache-control", "").split(",")}
            require("no-store" in directives, "MCP responses must not be cached")
            result = response.json()
            require("result" in result, "MCP protocol returned an error")
            return response, result["result"]
        initialized, result = rpc("initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                                  "clientInfo": {"name": "synthetic-trial-verifier", "version": "1"}})
        require(result["protocolVersion"] == "2025-11-25", "MCP version negotiation failed")
        headers.update({"MCP-Protocol-Version": "2025-11-25", "Mcp-Session-Id": initialized.headers["Mcp-Session-Id"]})
        notice = client.post(resource, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
        require(notice.status_code == 202, "MCP initialization failed")
        _, listed = rpc("tools/list")
        names = {tool["name"] for tool in listed["tools"]}
        require({"list_portfolios", "get_portfolio_state", "get_current_knowledge"} <= names, "Read tools unavailable")
        require(not any(name.startswith(("preview_", "apply_")) for name in names), "Write tools exposed")
        for name, arguments in (("list_portfolios", {}), ("get_portfolio_state", {"portfolio_id": "portfolio-alpha"}),
                                ("get_current_knowledge", {})):
            _, result = rpc("tools/call", {"name": name, "arguments": arguments})
            require(not result.get("isError"), "Synthetic read tool failed: " + name)
        for name, arguments in (("apply_knowledge_update", {}), ("preview_knowledge_update", {}),
                                ("get_portfolio_state", {"portfolio_id": "portfolio-beta"})):
            _, result = rpc("tools/call", {"name": name, "arguments": arguments})
            require(result.get("isError"), "Unauthorized tool call must fail")
        print("Authenticated MCP reads, blocked writes and portfolio isolation: PASS")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--ca", type=Path)
    parser.add_argument("--oauth-only", action="store_true")
    args = parser.parse_args()
    try:
        verify(args.out, args.ca, oauth_only=args.oauth_only)
    except RuntimeError as exc:
        print("Synthetic trial verification failed: " + str(exc), file=sys.stderr)
        return 2
    except Exception:
        # Do not print requests, login form actions, credentials or token bodies.
        print("Synthetic trial verification failed: network or configuration error", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
