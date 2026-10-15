"""Test browser CookieSession authorization with a separate synthetic Authelia.

Starts only a temporary loopback listener. Never reads an existing configuration,
changes ingress, or uses real credentials. Requires the optional remote packages.
"""
from __future__ import annotations

import argparse
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

from starlette.requests import Request

from protocol.v1.transport.mcp_confirmed_writes import AutheliaBrowserIdentity, ConfirmedWriteConfig


ORIGIN = "https://mcp.example.test"
TICKET = "a" * 43


def run(binary: Path) -> None:
    os.umask(0o077)
    with tempfile.TemporaryDirectory(prefix="investkitchen-approval-synthetic-") as directory:
        root = Path(directory)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        generated = subprocess.run([str(binary), "crypto", "hash", "generate", "argon2", "--random",
                                    "--random.length", "48", "--no-confirm"],
                                   capture_output=True, text=True, check=True)
        fields = {line.split(":", 1)[0].strip(): line.split(":", 1)[1].strip()
                  for line in generated.stdout.splitlines() if ":" in line}
        (root / "users.json").write_text(json.dumps({"users": {"synthetic-owner": {
            "displayname": "Synthetic owner", "password": fields["Digest"],
            "email": "synthetic@example.invalid", "groups": []}}}))
        configuration = {
            "server": {"address": f"tcp://127.0.0.1:{port}/"},
            "log": {"level": "warn", "format": "json"},
            "totp": {"disable": True}, "webauthn": {"disable": True}, "duo_api": {"disable": True},
            "authentication_backend": {"password_reset": {"disable": True},
                "file": {"path": str(root / "users.json"), "watch": False}},
            "access_control": {"default_policy": "deny", "rules": [{
                "domain": "mcp.example.test", "policy": "one_factor", "subject": ["user:synthetic-owner"],
                "methods": ["GET", "POST"], "resources": [r"^/approve/[A-Za-z0-9_-]{43}$"]}]},
            "session": {"secret": secrets.token_urlsafe(48), "expiration": "5 minutes", "cookies": [
                {"domain": "mcp.example.test", "authelia_url": ORIGIN}]},
            "storage": {"encryption_key": secrets.token_urlsafe(48), "local": {"path": str(root / "db.sqlite3")}},
            "notifier": {"filesystem": {"filename": str(root / "notifications.txt")}},
        }
        config_path = root / "configuration.json"
        config_path.write_text(json.dumps(configuration))
        subprocess.run([str(binary), "validate-config", "--config", str(config_path)],
                       capture_output=True, check=True)
        with (root / "service.log").open("wb") as log:
            process = subprocess.Popen([str(binary), "--config", str(config_path)], stdout=log, stderr=log)
            try:
                deadline = time.monotonic() + 20
                while True:
                    if process.poll() is not None:
                        raise RuntimeError("Synthetic authentication service exited")
                    try:
                        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                            break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise RuntimeError("Synthetic authentication service did not start") from None
                        time.sleep(0.1)
                config = ConfirmedWriteConfig.model_validate({
                    "authelia_authz_url": f"http://127.0.0.1:{port}/api/authz/forward-auth",
                    "grants": [{"subject": "synthetic-subject", "client_id": "synthetic-client",
                                "browser_username": "synthetic-owner", "permissions": ["knowledge.commit"],
                                "expires_at": "2099-01-01T00:00:00Z"}]})
                identity = AutheliaBrowserIdentity(config, ORIGIN)
                def request(cookie: str = "", path: str = "/approve/" + TICKET) -> Request:
                    return Request({"type": "http", "method": "GET", "scheme": "https", "path": path,
                                    "headers": [(b"host", b"mcp.example.test"), (b"cookie", cookie.encode()),
                                                (b"remote-user", b"synthetic-owner"),
                                                (b"authorization", b"Bearer synthetic-forgery")]})
                assert identity(request()) is None
                assert identity(request("authelia_session=forged-session")) is None
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    conn.request("POST", "/api/firstfactor", body=json.dumps({
                        "username": "synthetic-owner", "password": fields["Random Password"],
                        "keepMeLoggedIn": False, "targetURL": ORIGIN + "/approve/" + TICKET,
                        "requestMethod": "GET"}), headers={"Content-Type": "application/json",
                        "Host": "mcp.example.test", "X-Forwarded-Proto": "https",
                        "Origin": ORIGIN, "X-Forwarded-For": "127.0.0.1"})
                    response = conn.getresponse()
                    assert response.status == 200
                    cookies = SimpleCookie()
                    for key, value in response.getheaders():
                        if key.lower() == "set-cookie": cookies.load(value)
                    response.read()
                    cookie = "authelia_session=" + cookies["authelia_session"].value
                finally:
                    conn.close()
                assert identity(request(cookie)) == "synthetic-owner"
                assert identity(request(cookie, "/mcp")) is None
                assert identity(request(cookie, "/approve/short")) is None
                print("Synthetic Authelia approval: PASS (login, trusted cookie, spoof rejection, exact path ACL)")
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--authelia", type=Path, required=True)
    args = parser.parse_args()
    try:
        run(args.authelia.resolve())
    except Exception:
        print("Synthetic Authelia approval: FAIL (private test output withheld)")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
