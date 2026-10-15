"""Prepare an isolated synthetic trial; generated credentials stay outside Git."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import uuid
from pathlib import Path
from urllib.parse import urlsplit


def write(path: Path, value: str, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".new")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(value)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def document(path: Path, value: dict, *, mode: int = 0o600) -> None:
    write(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n", mode=mode)


def outside_git(root: Path) -> Path:
    root = root.expanduser().resolve()
    if any((parent / ".git").exists() for parent in (root, *root.parents)):
        raise ValueError("Trial files must be outside Git")
    if any(character.isspace() for character in str(root)) or any(c in str(root) for c in "#$'\"\\"):
        raise ValueError("Use a plain absolute trial directory")
    return root


def origin(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment
            or re.fullmatch(r"[A-Za-z0-9.-]+", parsed.hostname) is None):
        raise ValueError("Origin must be an absolute HTTPS origin")
    _ = parsed.port
    return "https://" + parsed.netloc


def redirect(value: str) -> str:
    parsed = urlsplit(value)
    if (not parsed.hostname or parsed.username or parsed.password or parsed.fragment
            or "*" in value or any(c.isspace() for c in value)
            or (parsed.scheme != "https" and not
                (parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}))):
        raise ValueError("Use an exact HTTPS or loopback callback URI")
    _ = parsed.port
    return value


def synthetic_bundle(root: Path) -> None:
    tp = {"value": "2026-10-14T00:00:00Z", "precision": "source_exact"}
    portfolio = {"protocol_version": "1.0-draft", "portfolio_id": "portfolio-alpha",
                 "display_name": "Synthetic Gemini Trial", "generated_at": tp,
                 "accounts": [], "positions": [], "cash": [], "transactions": [], "policies": [],
                 "completeness": {"holdings": "complete", "cash": "complete", "valuation": "unknown",
                                  "fx": "unknown", "transactions": "complete", "accounts": []},
                 "migration_gaps": []}
    current = {"contract_version": "1.0-draft", "capability": "knowledge.current",
               "producer": {"id": "investkitchen.synthetic-trial", "version": "0.1.0"}, "status": "ok",
               "generated_at": tp, "authority": "knowledge_claim", "freshness": "local",
               "source_mode": "fixture", "permissions_used": ["knowledge.read"],
               "data": {"generation": {"generation_id": "synthetic-generation", "commit_status": "committed",
                                       "committed_at": tp}, "summary": "Synthetic trial data only.", "outlook": []},
               "data_times": {}, "provenance": [], "warnings": ["Synthetic trial data only."],
               "gaps": [], "conflicts": []}
    personal = root / "synthetic/personal"
    files = []
    for filename, content_type, value in (("portfolio.json", "portfolio", portfolio),
                                          ("knowledge-current.json", "knowledge_current", current),
                                          ("knowledge-projection.json", "knowledge_projection", {"claims": [], "evidence": []})):
        document(personal / filename, value)
        raw = (personal / filename).read_bytes()
        entry = {"logical_name": filename, "relative_path": filename, "content_type": content_type,
                 "sha256": hashlib.sha256(raw).hexdigest(), "size_bytes": len(raw)}
        if content_type == "portfolio":
            entry["portfolio_id"] = "portfolio-alpha"
        files.append(entry)
    document(personal / "personal-data-manifest.json", {
        "protocol_version": "1.0-draft", "bundle_format_version": 1,
        "bundle_id": "synthetic-gemini-trial", "portfolio_ids": ["portfolio-alpha"], "files": files})
    # This trial owns these synthetic lock files; it never initializes live stores.
    for name in (".write-journal.lock", ".knowledge-journal.lock"):
        write(root / "synthetic/state/native-write" / name, "")


def bootstrap(root: Path, image: str, edge_image: str = "investkitchen-gemini-edge:trial") -> None:
    if root.exists() and any(root.iterdir()):
        raise ValueError("Use a new empty trial directory")
    if any(re.fullmatch(r"[a-z0-9][a-z0-9._/:@-]+", value) is None for value in (image, edge_image)):
        raise ValueError("Invalid image reference")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in ("edge", "auth", "auth/import", "oauth"):
        (root / name).mkdir(mode=0o750)
    write(root / "auth/keycloak.env", "")
    write(root / "edge/Caddyfile", "{\n admin 127.0.0.1:2019\n auto_https off\n}\n:8092 {\n bind 127.0.0.1\n respond \"InvestKitchen synthetic trial is not configured\" 503\n}\n", mode=0o644)
    write(root / "trial.env", f"GEMINI_PROJECT=investkitchen-gemini-trial-{secrets.token_hex(4)}\nGEMINI_TRIAL_ROOT={root}\nGEMINI_UID={os.getuid()}\nGEMINI_GID={os.getgid()}\nGEMINI_HTTP_IMAGE={image}\nGEMINI_EDGE_IMAGE={edge_image}\n")
    synthetic_bundle(root)


def configure(root: Path, public_origin: str, callbacks: list[str]) -> None:
    public_origin = origin(public_origin)
    callbacks = [redirect(uri) for uri in callbacks]
    if not callbacks or not (root / "trial.env").is_file():
        raise ValueError("Bootstrap first and supply an exact callback URI")
    if (root / "auth/import/investkitchen-realm.json").exists():
        raise ValueError("A configured trial cannot be overwritten; create a fresh trial")
    subject = str(uuid.uuid4())
    client_secret, password = secrets.token_urlsafe(32), secrets.token_urlsafe(24)
    realm = {"realm": "investkitchen", "enabled": True, "sslRequired": "all",
             "registrationAllowed": False, "resetPasswordAllowed": False,
             "bruteForceProtected": True, "failureFactor": 5, "accessTokenLifespan": 300,
             "defaultSignatureAlgorithm": "RS256",
             "clientScopes": [{"name": "investkitchen.read", "protocol": "openid-connect",
                               "attributes": {"include.in.token.scope": "true"}}],
             "clients": [{"clientId": "investkitchen-gemini", "protocol": "openid-connect",
                          "enabled": True, "publicClient": False, "secret": client_secret,
                          "standardFlowEnabled": True, "directAccessGrantsEnabled": False,
                          "serviceAccountsEnabled": False, "implicitFlowEnabled": False,
                          "redirectUris": callbacks, "webOrigins": [],
                          "attributes": {"pkce.code.challenge.method": "S256", "access.token.signed.response.alg": "RS256"},
                          "defaultClientScopes": ["investkitchen.read"],
                          "protocolMappers": [{"name": "InvestKitchen audience", "protocol": "openid-connect",
                                               "protocolMapper": "oidc-audience-mapper", "config": {
                                                   "included.custom.audience": public_origin + "/mcp",
                                                   "access.token.claim": "true", "id.token.claim": "false"}},
                                              {"name": "Authenticated subject", "protocol": "openid-connect",
                                               "protocolMapper": "oidc-sub-mapper",
                                               "config": {"access.token.claim": "true", "introspection.token.claim": "true"}}]}],
             "users": [{"id": subject, "username": "synthetic-owner", "enabled": True,
                        "firstName": "Synthetic", "lastName": "Owner",
                        "email": "synthetic@example.invalid", "emailVerified": True,
                        "credentials": [{"type": "password", "value": password, "temporary": False}]}]}
    document(root / "auth/import/investkitchen-realm.json", realm, mode=0o640)
    write(root / "auth/keycloak.env", "KC_HOSTNAME=" + public_origin + "\nKC_HOSTNAME_STRICT=true\nKC_PROXY_HEADERS=xforwarded\nKC_HTTP_ENABLED=true\n")
    document(root / "credentials.json", {"username": "synthetic-owner", "password": password,
                                          "client_id": "investkitchen-gemini", "client_secret": client_secret})
    document(root / "oauth/oauth-resource.json", {
        "issuer_url": public_origin + "/realms/investkitchen", "resource_url": public_origin + "/mcp",
        "client_id_claim": "azp", "grants": [{"subject": subject, "client_id": "investkitchen-gemini",
        "permissions": ["system.capabilities.read", "portfolio.read", "knowledge.read", "decision.context.build"],
        "portfolio_scope": ["portfolio-alpha"]}]})
    authority = urlsplit(public_origin).netloc
    host = urlsplit(public_origin).hostname
    routes = """(trial_routes) {
 header Cache-Control no-store
 @mcp path /mcp /.well-known/oauth-protected-resource/mcp
 handle @mcp {
  request_body {
   max_size 64KB
  }
  reverse_proxy 127.0.0.1:8090
 }
 @discovery path /.well-known/oauth-authorization-server/realms/investkitchen
 handle @discovery {
  rewrite * /realms/investkitchen/.well-known/openid-configuration
  reverse_proxy 127.0.0.1:8091 {
   header_up X-Forwarded-Proto https
   header_up X-Forwarded-Host PUBLIC_AUTHORITY
   header_up X-Forwarded-Port PUBLIC_PORT
  }
 }
 @auth path /realms/investkitchen/.well-known/* /realms/investkitchen/protocol/openid-connect/* /realms/investkitchen/login-actions/* /resources/*
 handle @auth {
  request_body {
   max_size 32KB
  }
  reverse_proxy 127.0.0.1:8091 {
   header_up X-Forwarded-Proto https
   header_up X-Forwarded-Host PUBLIC_AUTHORITY
   header_up X-Forwarded-Port PUBLIC_PORT
  }
 }
 handle {
  respond 404
 }
}
""".replace("PUBLIC_AUTHORITY", authority).replace("PUBLIC_PORT", str(urlsplit(public_origin).port or 443))
    settings = "{\n admin 127.0.0.1:2019\n auto_https off\n}\n"
    write(root / "edge/Caddyfile", settings + routes + f"http://{host}:8092 {{\n bind 127.0.0.1\n import trial_routes\n}}\n", mode=0o644)
    # Local integration uses real TLS with a private test CA; no public tunnel.
    local_settings = settings.replace("auto_https off", "auto_https disable_redirects\n skip_install_trust")
    write(root / "edge/Caddyfile.local", local_settings + routes + "https://localhost:8443 {\n bind 0.0.0.0\n tls internal\n import trial_routes\n}\n", mode=0o644)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("bootstrap", "configure"))
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--image")
    parser.add_argument("--edge-image", default="investkitchen-gemini-edge:trial")
    parser.add_argument("--origin")
    parser.add_argument("--redirect-uri", action="append", default=[])
    args = parser.parse_args()
    try:
        root = outside_git(args.out)
        if args.command == "bootstrap":
            if not args.image:
                raise ValueError("Supply a verified commit image")
            bootstrap(root, args.image, args.edge_image)
        else:
            if not args.origin:
                raise ValueError("Supply the assigned HTTPS origin")
            configure(root, args.origin, args.redirect_uri)
    except OSError:
        print("InvestKitchen synthetic trial configuration failed: filesystem unavailable", file=sys.stderr)
        return 2
    except ValueError as exc:
        # No file contents, credentials or traceback are emitted.
        print("InvestKitchen synthetic trial configuration failed: " + str(exc).split(":")[0], file=sys.stderr)
        return 2
    print("InvestKitchen synthetic trial configuration: PASS (credentials kept in external files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
