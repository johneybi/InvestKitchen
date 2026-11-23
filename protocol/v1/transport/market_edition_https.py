"""Private, mutually authenticated HTTPS entry point for KR preopen editions."""

import argparse
import base64
from datetime import datetime, timezone
from functools import partial
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
import os
from pathlib import Path
import re
import socket
import sqlite3
import ssl
import time

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.hazmat.primitives.serialization import load_pem_public_key

from ..runtime.market_edition_store import IngestError, MAX_PAYLOAD, MarketEditionStore, decode_object

ROUTE = re.compile(r"/internal/v1/market-editions/([0-9a-f-]{36})")
POLICY_FIELDS = {
    "policy_id", "version", "active", "instance_id", "action", "market", "kind",
    "publisher_id", "scope_id", "certificate_sha256", "not_before", "expires_at",
}


class PublishApproval:
    """Check the current NAS-operator-signed grant on each authorization."""

    def __init__(self, policy, trust_key, instance_id):
        self.policy = policy
        self.key = load_pem_public_key(trust_key.read_bytes())
        if not isinstance(self.key, Ed25519PublicKey):
            raise ValueError("expected the operator's Ed25519 public key")
        self.instance_id = instance_id

    def __call__(self, certificate):
        try:
            with self.policy.open("rb") as stream:
                envelope = decode_object(stream.read(16385))
            if set(envelope) != {"policy", "signature"}:
                raise ValueError("unexpected approval wrapper")
            raw = base64.b64decode(envelope["policy"], validate=True)
            self.key.verify(base64.b64decode(envelope["signature"], validate=True), raw)
            grant = decode_object(raw)
            if set(grant) != POLICY_FIELDS:
                raise ValueError("unexpected approval fields")
            starts = datetime.fromisoformat(grant["not_before"])
            expires = datetime.fromisoformat(grant["expires_at"])
            now = datetime.now(timezone.utc)
            ids_valid = all(isinstance(grant[field], str) and
                            re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", grant[field])
                            for field in ("policy_id", "publisher_id", "scope_id"))
            if (type(grant["version"]) is not int or grant["version"] != 1
                    or grant["active"] is not True or not ids_valid
                    or grant["instance_id"] != self.instance_id
                    or (grant["action"], grant["market"], grant["kind"]) !=
                    ("market_edition.publish", "KR", "preopen")
                    or grant["certificate_sha256"] != sha256(certificate).hexdigest()
                    or not starts <= now < expires):
                raise ValueError("inactive or out-of-scope grant")
        except (OSError, ValueError, TypeError, RecursionError, InvalidSignature) as exc:
            raise IngestError(403, "publish_not_authorized") from exc
        return grant


class MarketEditionHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        # Avoid logging production identifiers and request data.
        pass

    def _json(self, status, value):
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_PUT(self):
        try:
            route = ROUTE.fullmatch(self.path)
            if not route:
                raise IngestError(404, "not_found")
            cert = self.connection.getpeercert(binary_form=True)
            authorize = partial(self.server.approval, cert)
            authorize()
            statuses = self.headers.get_all("Content-Length", [])
            if (self.headers.get_all("Content-Type") != ["application/json"]
                    or "Content-Encoding" in self.headers or "Transfer-Encoding" in self.headers
                    or len(statuses) != 1 or not re.fullmatch(r"[0-9]{1,10}", statuses[0])):
                raise IngestError(400, "invalid_headers")
            size = int(statuses[0])
            if size > MAX_PAYLOAD:
                raise IngestError(413, "payload_too_large")
            hashes = self.headers.get_all("X-Payload-SHA256", [])
            if len(hashes) != 1 or not re.fullmatch(r"[0-9a-f]{64}", hashes[0]):
                raise IngestError(400, "invalid_payload_hash")
            raw = self._read_body(size)
            status, payload = self.server.store.accept(route[1], hashes[0], raw, authorize)
        except IngestError as exc:
            status, payload = exc.status, exc.payload
        except (sqlite3.Error, OSError):
            status, payload = 503, {"error": "unavailable"}
        self._json(status, payload)

    def _read_body(self, size):
        raw = bytearray()
        deadline = time.monotonic() + 15
        while len(raw) < size:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise IngestError(400, "incomplete_body")
            self.connection.settimeout(remaining)
            try:
                part = self.rfile.read1(size - len(raw))
            except TimeoutError as exc:
                raise IngestError(400, "incomplete_body") from exc
            if not part:
                raise IngestError(400, "incomplete_body")
            raw.extend(part)
        return bytes(raw)


class MarketEditionHTTPS(HTTPServer):
    def __init__(self, address, context, store, approval):
        self.context = context
        self.store = store
        self.approval = approval
        super().__init__(address, MarketEditionHandler)

    def get_request(self):
        conn, address = super().get_request()
        try:
            conn.settimeout(8)
            return self.context.wrap_socket(conn, server_side=True), address
        except OSError:
            conn.close()
            raise


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bind", required=True, help="Dedicated LAN IPv4 address, or loopback for preflight")
    parser.add_argument("--container-network", action="store_true",
                        help="Allow wildcard only inside the isolated Compose network")
    parser.add_argument("--port", required=True, type=int)
    parser.add_argument("--instance-id", required=True)
    for field in ("db", "server-cert", "server-key", "publisher-ca", "operator-key", "policy"):
        parser.add_argument("--" + field, required=True, type=Path)
    args = parser.parse_args()
    if args.bind == "0.0.0.0" and not args.container_network:
        parser.error("a specific listen address is required")
    try:
        socket.inet_pton(socket.AF_INET, args.bind)
    except OSError:
        parser.error("bind must be a specific IPv4 address")

    os.umask(0o077)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.verify_flags |= ssl.VERIFY_X509_STRICT
    context.load_cert_chain(args.server_cert, args.server_key)
    context.load_verify_locations(cafile=str(args.publisher_ca))
    approval = PublishApproval(args.policy, args.operator_key, args.instance_id)
    store = MarketEditionStore(args.db)
    store.initialize()
    with MarketEditionHTTPS((args.bind, args.port), context, store, approval) as server:
        print("market edition HTTPS ready", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
