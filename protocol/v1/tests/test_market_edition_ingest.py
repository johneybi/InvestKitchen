"""Synthetic coverage for approval, transactions, and HTTPS delivery."""

from contextlib import closing
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from http.client import HTTPSConnection
import importlib.util
import ipaddress
import json
from pathlib import Path
import sqlite3
import ssl
import subprocess
import sys
from threading import Thread

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, PublicFormat
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
import pytest

from protocol.v1.runtime.market_edition_reader import MarketEditionReader
from protocol.v1.runtime.market_edition_store import IngestError, MarketEditionStore
from protocol.v1.transport.market_edition_https import MarketEditionHTTPS, PublishApproval

EDITION_ID = "10000000-0000-4000-8000-000000000001"
SNAPSHOT_ID = "20000000-0000-4000-8000-000000000001"


@pytest.fixture
def edition():
    return {
        "schema_version": 1, "edition_id": EDITION_ID, "market": "KR", "kind": "preopen",
        "trade_date": "2026-11-20", "parent_edition_id": None,
        "published_at": "2026-11-20T07:04:00+09:00",
        "source_cutoff_at": "2026-11-20T07:00:00+09:00",
        "snapshot": {"snapshot_id": SNAPSHOT_ID, "scope": "Fixture", "screened_count": 1,
                     "coverage": "complete", "gaps": [], "sources": [
                         {"source_id": "s1", "publisher": "Fixture", "title": "Fixture",
                          "known_at": "2026-11-20T06:52:00+09:00", "excerpt": "Synthetic",
                          "url": "https://example.org/disclosures/fixture"}]},
        "report": {"title": "Fixture", "summary": "Fixture", "themes": [],
                   "body_markdown": "합성", "candidates": [
                       {"candidate_id": "KRX:000001", "name": "Fixture", "status": "watch",
                        "reason": "Fixture", "recheck": "Fixture", "evidence": [
                            {"type": "fact", "text": "Fixture", "source_ids": ["s1"]}]}]},
    }


@pytest.fixture
def store(tmp_path):
    db = MarketEditionStore(tmp_path / "editions.sqlite")
    db.initialize()
    return db


def encode(value):
    return json.dumps(value, ensure_ascii=False).encode()


def grant():
    return {"scope_id": "research", "publisher_id": "fixture-publisher"}


def read_database(store):
    with closing(sqlite3.connect(store.database.as_uri() + "?mode=ro", uri=True)) as db:
        counts = [db.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                  for table in ("snapshots", "editions", "candidates")]
        stored = db.execute("SELECT payload, payload_sha256, revision, accepted_at FROM editions").fetchall()
    return counts, stored


def test_revoke_before_commit_rolls_back_all_tables(store, edition):
    raw = encode(edition)
    calls = iter([grant(), None])

    def authorize():
        value = next(calls)
        if value is None:
            assert read_database(store) == ([0, 0, 0], [])
            raise IngestError(403, "publish_not_authorized")
        return value

    with pytest.raises(IngestError, match="publish_not_authorized"):
        store.accept(EDITION_ID, sha256(raw).hexdigest(), raw, authorize)
    assert read_database(store) == ([0, 0, 0], [])
    assert store.accept(EDITION_ID, sha256(raw).hexdigest(), raw, grant)[0] == 201


def test_writer_termination_recovers_without_partial_edition(store, edition):
    raw = encode(edition)
    digest = sha256(raw).hexdigest()
    first_code, first = store.accept(EDITION_ID, digest, raw, grant)
    assert first_code == 201
    edition["edition_id"] = EDITION_ID[:-1] + "3"
    edition["parent_edition_id"] = EDITION_ID
    other = encode(edition)
    script = """
import json, os, sys
from hashlib import sha256
from pathlib import Path
from protocol.v1.runtime.market_edition_store import MarketEditionStore

count = 0
def authorize():
    global count
    count += 1
    if count == 2:
        os._exit(44)
    return {"scope_id": "research", "publisher_id": "fixture-publisher"}

raw = sys.stdin.buffer.read()
MarketEditionStore(Path(sys.argv[1])).accept(
    json.loads(raw)["edition_id"], sha256(raw).hexdigest(), raw, authorize)
"""
    result = subprocess.run([sys.executable, "-c", script, str(store.database)],
                            input=other, check=False)
    assert result.returncode == 44
    assert read_database(store) == ([1, 1, 1], [(raw, digest, 1, first["accepted_at"])])
    store.initialize()
    assert store.accept(edition["edition_id"], sha256(other).hexdigest(), other, grant)[0] == 201
    assert read_database(store)[0] == [1, 2, 2]


def test_revision_and_snapshot_conflicts(store, edition):
    def accept():
        raw = encode(edition)
        return store.accept(edition["edition_id"], sha256(raw).hexdigest(), raw, grant)

    first = accept()
    first_bytes = encode(edition)
    edition["edition_id"] = EDITION_ID[:-1] + "2"
    with pytest.raises(IngestError, match="head_conflict"):
        accept()
    edition["parent_edition_id"] = EDITION_ID
    edition["snapshot"]["scope"] = "changed snapshot"
    with pytest.raises(IngestError, match="snapshot_conflict"):
        accept()
    edition["snapshot"]["scope"] = "Fixture"
    assert accept()[1]["revision"] == 2
    assert store.accept(EDITION_ID, sha256(first_bytes).hexdigest(), first_bytes, grant) == (200, first[1])
    assert read_database(store)[0] == [1, 2, 2]


def test_invalid_semantic_inputs_leave_no_rows(store, edition):
    def rejected(code):
        raw = encode(edition)
        with pytest.raises(IngestError, match=code):
            store.accept(EDITION_ID, sha256(raw).hexdigest(), raw, grant)
        assert read_database(store) == ([0, 0, 0], [])

    source = edition["snapshot"]["sources"][0]
    source["url"] = "https://192.168.0.12/source"
    rejected("invalid_source")
    source["url"] = "https://example.org/source"
    source["known_at"] = "2026-11-20T07:00:00.00000001+09:00"
    rejected("invalid_source")
    source["known_at"] = "2026-11-20T07:00:00+09:00"
    edition["report"]["candidates"][0]["evidence"][0]["source_ids"] = ["missing"]
    rejected("unknown_source")
    edition["report"]["body_markdown"] = "가" * 90000
    rejected("report_too_large")


def certificates(tmp_path):
    now = datetime.now(timezone.utc)
    ca_key = Ed25519PrivateKey.generate()
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Synthetic CA")])

    def issue(name, issuer, key):
        return (x509.CertificateBuilder().subject_name(name).issuer_name(issuer)
                .serial_number(x509.random_serial_number()).public_key(key.public_key())
                .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(hours=1))
                .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
                .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                               critical=False))

    ca = issue(ca_name, ca_name, ca_key).add_extension(
        x509.BasicConstraints(ca=True, path_length=0), critical=True).add_extension(
            x509.KeyUsage(False, False, False, False, False, True, True, False, False),
            critical=True).sign(ca_key, None)
    (tmp_path / "ca.pem").write_bytes(ca.public_bytes(Encoding.PEM))

    def leaf(label, usage):
        key = Ed25519PrivateKey.generate()
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, label)])
        cert = (issue(name, ca_name, key)
                .add_extension(x509.ExtendedKeyUsage([usage]), critical=False)
                .add_extension(x509.KeyUsage(True, False, False, False, False, False, False, False, False),
                               critical=True)
                .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]),
                               critical=False).sign(ca_key, None))
        (tmp_path / f"{label}.pem").write_bytes(cert.public_bytes(Encoding.PEM))
        (tmp_path / f"{label}-key.pem").write_bytes(
            key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
        return cert

    leaf("nas", ExtendedKeyUsageOID.SERVER_AUTH)
    leaf("unapproved", ExtendedKeyUsageOID.CLIENT_AUTH)
    return leaf("publisher", ExtendedKeyUsageOID.CLIENT_AUTH)


def test_https_signed_approval_and_windows_sender(tmp_path, store, edition):
    from base64 import b64encode

    issued = certificates(tmp_path)
    key = Ed25519PrivateKey.generate()
    trust = tmp_path / "operator.pem"
    trust.write_bytes(key.public_key().public_bytes(Encoding.PEM, PublicFormat.SubjectPublicKeyInfo))
    policy = tmp_path / "grant.json"
    now = datetime.now(timezone.utc)
    allowed = {**grant(), "policy_id": "fixture", "version": 1, "active": True,
               "instance_id": "fixture", "action": "market_edition.publish", "market": "KR",
               "kind": "preopen", "certificate_sha256": sha256(issued.public_bytes(Encoding.DER)).hexdigest(),
               "not_before": (now - timedelta(minutes=1)).isoformat(),
               "expires_at": (now + timedelta(minutes=1)).isoformat()}

    def write_policy(data, sign_with=key):
        raw = encode(data)
        policy.write_bytes(encode({"policy": b64encode(raw).decode(),
                                   "signature": b64encode(sign_with.sign(raw)).decode()}))

    server_tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_tls.load_cert_chain(tmp_path / "nas.pem", tmp_path / "nas-key.pem")
    server_tls.load_verify_locations(cafile=str(tmp_path / "ca.pem"))
    server_tls.verify_mode = ssl.CERT_REQUIRED
    server_tls.verify_flags |= ssl.VERIFY_X509_STRICT
    client_tls = ssl.create_default_context(cafile=str(tmp_path / "ca.pem"))
    client_tls.load_cert_chain(tmp_path / "publisher.pem", tmp_path / "publisher-key.pem")
    other_tls = ssl.create_default_context(cafile=str(tmp_path / "ca.pem"))
    other_tls.load_cert_chain(tmp_path / "unapproved.pem", tmp_path / "unapproved-key.pem")
    anonymous_tls = ssl.create_default_context(cafile=str(tmp_path / "ca.pem"))

    with MarketEditionHTTPS(("127.0.0.1", 0), server_tls, store,
                            PublishApproval(policy, trust, "fixture")) as server:
        worker = Thread(target=server.serve_forever, daemon=True)
        worker.start()
        raw = encode(edition)
        digest = sha256(raw).hexdigest()

        def put(data=raw, path_id=EDITION_ID, checksum=None, context=client_tls):
            with closing(HTTPSConnection("127.0.0.1", server.server_port, context=context)) as conn:
                conn.request("PUT", f"/internal/v1/market-editions/{path_id}", data, {
                    "Content-Type": "application/json",
                    "X-Payload-SHA256": checksum or sha256(data).hexdigest(),
                })
                response = conn.getresponse()
                return response.status, json.loads(response.read())

        try:
            with pytest.raises((OSError, ssl.SSLError)):
                put(context=anonymous_tls)
            assert put()[0] == 403
            write_policy(allowed, Ed25519PrivateKey.generate())
            assert put()[0] == 403
            write_policy({**allowed, "active": False})
            assert put()[0] == 403
            write_policy({**allowed, "expires_at": now.isoformat()})
            assert put()[0] == 403
            write_policy(allowed)
            assert put(context=other_tls)[0] == 403
            assert put(checksum="a" * 64) == (400, {"error": "invalid_payload_hash"})
            assert put(path_id=EDITION_ID[:-1] + "5") == (400, {"error": "edition_id_mismatch"})
            assert read_database(store) == ([0, 0, 0], [])

            sender_path = Path(__file__).resolve().parents[3] / "deploy/windows/send-market-edition.py"
            spec = importlib.util.spec_from_file_location("market_edition_sender", sender_path)
            sender = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(sender)
            origin = f"https://127.0.0.1:{server.server_port}"
            status, receipt = sender.send(origin, client_tls, raw, EDITION_ID, digest, 1)
            assert status == 201
            sender.verify(receipt, edition, digest, grant()["publisher_id"])
            receipt_path = tmp_path / "receipt.json"
            sender.record(receipt_path, receipt)
            assert put(raw + b"\n") == (409, {"error": "edition_id_conflict"})
            status, retry = sender.send(origin, client_tls, raw, EDITION_ID, digest, 1)
            assert status == 200
            assert retry == receipt
            sender.record(receipt_path, retry)
            assert json.loads(receipt_path.read_bytes()) == receipt
            reader = MarketEditionReader(store.database, scope_id="research",
                                         viewer_editions_url="https://example.org/editions")
            assert reader.get_market_edition(edition_id=EDITION_ID)["candidate_count"] == 1
            assert read_database(store) == ([1, 1, 1], [(raw, digest, 1, receipt["accepted_at"])])
        finally:
            server.shutdown()
            worker.join(timeout=2)
