"""Send an existing edition byte-for-byte and persist the verified NAS receipt."""

import argparse
from datetime import datetime
from hashlib import sha256
from http.client import HTTPSConnection, HTTPException
import json
import os
from pathlib import Path
import re
import ssl
import tempfile
import time
from urllib.parse import urlsplit

MAX_REQUEST = 2 * 1024 * 1024
MAX_RECEIPT = 8192


class DeliveryError(Exception):
    pass


def send(url, context, raw, edition_id, digest, attempts):
    address = urlsplit(url)
    if (address.scheme != "https" or not address.hostname or address.username is not None
            or address.path not in ("", "/") or address.query or address.fragment):
        raise DeliveryError("NAS URL must be an HTTPS origin")
    for attempt in range(attempts):
        connection = HTTPSConnection(address.hostname, address.port, context=context, timeout=20)
        try:
            connection.request("PUT", f"/internal/v1/market-editions/{edition_id}", raw, {
                "Content-Type": "application/json", "X-Payload-SHA256": digest,
            })
            response = connection.getresponse()
            if response.status in (200, 201):
                if response.length is None or response.length > MAX_RECEIPT:
                    raise DeliveryError("invalid NAS receipt length")
                return response.status, json.loads(response.read())
            if response.status != 503:
                raise DeliveryError(f"NAS rejected delivery (HTTP {response.status})")
            failure = "NAS temporarily unavailable (HTTP 503)"
        except ssl.SSLCertVerificationError as exc:
            raise DeliveryError("NAS certificate verification failed") from exc
        except ssl.SSLError as exc:
            raise DeliveryError("mutual TLS connection failed") from exc
        except (OSError, HTTPException):
            failure = "delivery interrupted before a verified receipt arrived"
        finally:
            connection.close()
        if attempt + 1 < attempts:
            time.sleep(2 ** attempt)
    raise DeliveryError(failure)


def verify(receipt, edition, digest, publisher_id):
    expected = {key: edition[key] for key in
                ("edition_id", "parent_edition_id", "market", "trade_date", "kind")}
    expected.update(snapshot_id=edition["snapshot"]["snapshot_id"], payload_sha256=digest,
                    publisher_id=publisher_id)
    if (not isinstance(receipt, dict) or set(receipt) != expected.keys() | {"revision", "accepted_at"}
            or any(receipt[key] != value for key, value in expected.items())
            or type(receipt["revision"]) is not int or receipt["revision"] < 1
            or (edition["parent_edition_id"] is None) != (receipt["revision"] == 1)
            or datetime.fromisoformat(receipt["accepted_at"]).utcoffset() is None):
        raise DeliveryError("NAS receipt does not match the edition")


def record(path, receipt):
    raw = (json.dumps(receipt, sort_keys=True, indent=2) + "\n").encode("utf-8")
    with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
        temp = Path(stream.name)
        try:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            temp.unlink()
            raise
    try:
        # Same-volume hard link publishes a complete receipt without replacing
        # the first receipt, even when another Windows sender is running.
        try:
            os.link(temp, path)
        except FileExistsError:
            if json.loads(path.read_bytes()) != receipt:
                raise DeliveryError("NAS receipt differs from the original local receipt")
    finally:
        temp.unlink()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--nas", required=True)
    parser.add_argument("--publisher-id", required=True)
    parser.add_argument("--attempts", type=int, choices=range(1, 6), default=3)
    parser.add_argument("--expect-sha256")
    for field in ("input", "receipt", "nas-ca", "publisher-cert", "publisher-key"):
        parser.add_argument("--" + field, required=True, type=Path)
    args = parser.parse_args()
    try:
        if args.input.resolve() == args.receipt.resolve():
            raise DeliveryError("receipt path must differ from the edition path")
        with args.input.open("rb") as stream:
            raw = stream.read(MAX_REQUEST + 1)
        if len(raw) > MAX_REQUEST:
            raise DeliveryError("edition exceeds the request limit")
        digest = sha256(raw).hexdigest()
        if args.expect_sha256 and args.expect_sha256 != digest:
            raise DeliveryError("local file differs from the expected SHA-256")
        edition = json.loads(raw.decode("utf-8"))
        edition_id = edition["edition_id"]
        if not re.fullmatch(r"[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}", edition_id):
            raise DeliveryError("invalid edition ID")
        context = ssl.create_default_context(cafile=str(args.nas_ca))
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(args.publisher_cert, args.publisher_key)
        status, receipt = send(args.nas, context, raw, edition_id, digest, args.attempts)
        verify(receipt, edition, digest, args.publisher_id)
        record(args.receipt, receipt)
    except (OSError, ValueError, KeyError, TypeError, DeliveryError) as exc:
        parser.exit(1, f"Delivery failed: {exc}\n")
    print(json.dumps({"http_status": status, "request_bytes": len(raw), "receipt": receipt}))


if __name__ == "__main__":
    main()
