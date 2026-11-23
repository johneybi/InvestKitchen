"""Validate and commit a market edition and its persistent receipt."""

from contextlib import closing
from datetime import datetime, timezone
from hashlib import sha256
import ipaddress
import json
from pathlib import Path
import re
import sqlite3
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator, FormatChecker

MAX_PAYLOAD = 2 * 1024 * 1024
MAX_BODY = 256 * 1024
STREAM_FIELDS = ("market", "trade_date", "kind")
RECEIPT_FIELDS = (
    "edition_id", "revision", "parent_edition_id", *STREAM_FIELDS,
    "publisher_id", "snapshot_id", "payload_sha256", "accepted_at",
)
SCHEMA_PATH = Path(__file__).resolve().parents[1] / "schemas" / "market-edition-ingest.schema.json"

DDL = """
CREATE TABLE IF NOT EXISTS snapshots (
    scope_id TEXT NOT NULL, snapshot_id TEXT NOT NULL,
    cutoff TEXT NOT NULL, snapshot TEXT NOT NULL,
    PRIMARY KEY (scope_id, snapshot_id)
);
CREATE TABLE IF NOT EXISTS editions (
    scope_id TEXT NOT NULL, edition_id TEXT NOT NULL,
    market TEXT NOT NULL, trade_date TEXT NOT NULL, kind TEXT NOT NULL,
    revision INTEGER NOT NULL, parent_edition_id TEXT, publisher_id TEXT NOT NULL,
    snapshot_id TEXT NOT NULL, payload_sha256 TEXT NOT NULL, accepted_at TEXT NOT NULL,
    cutoff TEXT NOT NULL, payload BLOB NOT NULL,
    PRIMARY KEY (scope_id, edition_id),
    UNIQUE (scope_id, market, trade_date, kind, revision),
    FOREIGN KEY (scope_id, snapshot_id) REFERENCES snapshots (scope_id, snapshot_id),
    FOREIGN KEY (scope_id, parent_edition_id) REFERENCES editions (scope_id, edition_id)
);
CREATE TABLE IF NOT EXISTS candidates (
    scope_id TEXT NOT NULL, edition_id TEXT NOT NULL, candidate_id TEXT NOT NULL,
    position INTEGER NOT NULL, candidate TEXT NOT NULL,
    PRIMARY KEY (scope_id, edition_id, candidate_id),
    UNIQUE (scope_id, edition_id, position),
    FOREIGN KEY (scope_id, edition_id) REFERENCES editions (scope_id, edition_id)
);
"""


class IngestError(Exception):
    def __init__(self, status, code, **details):
        super().__init__(code)
        self.status = status
        self.payload = {"error": code, **details}


def _unique_object(pairs):
    result = dict(pairs)
    if len(result) != len(pairs):
        raise ValueError("duplicate JSON key")
    return result


def _reject_constant(value):
    raise ValueError("invalid JSON constant")


def decode_object(raw):
    value = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                       parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise ValueError("expected JSON object")
    return value


def _json(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _instant(value):
    parts = re.fullmatch(r"(.{19})(?:\.([0-9]+))?(Z|[+-][0-9]{2}:[0-9]{2})", value)
    seconds, fraction, offset = parts.groups()
    utc = datetime.fromisoformat(seconds + offset.replace("Z", "+00:00")).astimezone(timezone.utc)
    return utc, (fraction or "").rstrip("0")


def _public_https(value):
    url = urlsplit(value)
    if (url.scheme != "https" or not url.hostname or url.username is not None
            or "\\" in value or url.port == 0):
        return False
    host = url.hostname.rstrip(".")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return bool(re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z][a-z0-9-]*", host)
                    and host.rsplit(".", 1)[-1] not in {"local", "localhost", "internal",
                                                       "lan", "corp", "home", "onion",
                                                       "invalid", "test", "example"}
                    and not host.endswith(".home.arpa"))
    return address.is_global and not address.is_multicast


def _valid_document_ref(value):
    if not value or value.startswith((".", "~")) or re.match(r"^[a-z][a-z0-9+.-]*://", value, re.I):
        return False
    return not re.search(r'[/\\\x00-\x1f\x7f]|^[a-z]:', value, flags=re.I)


class MarketEditionStore:
    def __init__(self, database):
        self.database = database
        formats = FormatChecker()
        required = {"date", "date-time", "uuid", "uri"}
        if not required.issubset(formats.checkers):
            raise RuntimeError("install the market edition JSON Schema format dependencies")
        self.validator = Draft202012Validator(json.loads(SCHEMA_PATH.read_text()), format_checker=formats)

    def _connect(self):
        db = sqlite3.connect(self.database, timeout=5)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def initialize(self):
        with closing(self._connect()) as db:
            if db.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise RuntimeError("market edition database requires rollback journaling")
            with db:
                db.executescript("BEGIN IMMEDIATE;\n" + DDL)
                if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                    raise RuntimeError("market edition database is inconsistent")

    def _validate(self, path_id, digest, raw):
        if len(raw) > MAX_PAYLOAD:
            raise IngestError(413, "payload_too_large")
        if not re.fullmatch(r"[0-9a-f]{64}", digest) or sha256(raw).hexdigest() != digest:
            raise IngestError(400, "invalid_payload_hash")
        try:
            payload = decode_object(raw)
        except (ValueError, RecursionError) as exc:
            raise IngestError(400, "invalid_json") from exc
        if payload.get("edition_id") != path_id:
            raise IngestError(400, "edition_id_mismatch")
        try:
            # Reject escaped unpaired surrogates before indexing or returning strings.
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
            if not self.validator.is_valid(payload):
                raise IngestError(422, "invalid_schema")
            cutoff = _instant(payload["source_cutoff_at"])
            if cutoff > _instant(payload["published_at"]):
                raise IngestError(422, "invalid_cutoff")
            self._validate_report(payload, cutoff)
        except (ValueError, OverflowError, RecursionError) as exc:
            raise IngestError(422, "invalid_payload") from exc
        return payload

    @staticmethod
    def _validate_report(payload, cutoff):
        report = payload["report"]
        if len(report["body_markdown"].encode("utf-8")) > MAX_BODY:
            raise IngestError(413, "report_too_large")
        sources = {}
        for source in payload["snapshot"]["sources"]:
            key = source["source_id"]
            if (key in sources or _instant(source["known_at"]) > cutoff
                    or ("url" in source and not _public_https(source["url"]))
                    or ("document_ref" in source and not _valid_document_ref(source["document_ref"]))):
                raise IngestError(422, "invalid_source")
            sources[key] = source
        candidates = report["candidates"]
        if len({item["candidate_id"] for item in candidates}) != len(candidates):
            raise IngestError(422, "duplicate_candidate")
        # Bound the largest complete lookup row using conservative room for receipt
        # metadata, pagination fields, and the configured viewer URL.
        summary = {key: report[key] for key in ("title", "summary", "themes")}
        summary.update({key: payload["snapshot"][key] for key in ("scope", "gaps")})
        summary.update({key: payload[key] for key in ("source_cutoff_at", "published_at")})

        def wire_size(value):
            return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

        summary_room = 32768 - wire_size(summary) - 4096
        if summary_room < 0:
            raise IngestError(422, "summary_page_too_large")
        for candidate in candidates:
            header = {key: candidate[key] for key in ("candidate_id", "name", "status", "reason", "recheck")}
            if wire_size(header) > summary_room:
                raise IngestError(422, "candidate_page_too_large")
            header.update(source_cutoff_at=payload["source_cutoff_at"], evidence_gap=candidate.get("evidence_gap"))
            detail_room = 32768 - wire_size(header) - 1024
            if detail_room < 0:
                raise IngestError(422, "evidence_page_too_large")
            for evidence in candidate["evidence"]:
                try:
                    detail = {"type": evidence["type"], "text": evidence["text"],
                              "sources": [sources[key] for key in evidence["source_ids"]]}
                except KeyError as exc:
                    raise IngestError(422, "unknown_source") from exc
                if wire_size(detail) > detail_room:
                    raise IngestError(422, "evidence_page_too_large")

    def accept(self, path_id, digest, raw, authorize):
        grant = authorize()
        payload = self._validate(path_id, digest, raw)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            status, receipt = self._commit_rows(db, grant, payload, digest, raw)
            if authorize() != grant:
                raise IngestError(403, "grant_changed")
        return status, receipt

    @staticmethod
    def _commit_rows(db, grant, payload, digest, raw):
        edition_id = payload["edition_id"]
        scope = grant["scope_id"]
        publisher = grant["publisher_id"]
        scope_edition = (scope, edition_id)
        previous = db.execute("SELECT * FROM editions WHERE scope_id=? AND edition_id=?", scope_edition).fetchone()
        if previous:
            if previous["publisher_id"] != publisher:
                raise IngestError(403, "wrong_publisher")
            if previous["payload_sha256"] != digest or previous["payload"] != raw:
                raise IngestError(409, "edition_id_conflict")
            return 200, {key: previous[key] for key in RECEIPT_FIELDS}

        stream = tuple(payload[key] for key in STREAM_FIELDS)
        head = db.execute(
            "SELECT edition_id, revision, cutoff FROM editions WHERE scope_id=? AND market=? "
            "AND trade_date=? AND kind=? ORDER BY revision DESC LIMIT 1", (scope, *stream),
        ).fetchone()
        head_id = head["edition_id"] if head else None
        parent = payload["parent_edition_id"]
        if parent != head_id:
            raise IngestError(409, "head_conflict", head_edition_id=head_id)
        cutoff = payload["source_cutoff_at"]
        if head and _instant(cutoff) < _instant(head["cutoff"]):
            raise IngestError(422, "cutoff_before_parent")

        snapshot = payload["snapshot"]
        snapshot_id = snapshot["snapshot_id"]
        prior_snapshot = db.execute("SELECT cutoff, snapshot FROM snapshots WHERE scope_id=? AND snapshot_id=?",
                                    (scope, snapshot_id)).fetchone()
        if prior_snapshot:
            if (_instant(prior_snapshot["cutoff"]) != _instant(cutoff)
                    or json.loads(prior_snapshot["snapshot"]) != snapshot):
                raise IngestError(409, "snapshot_conflict")
        else:
            db.execute("INSERT INTO snapshots VALUES (?, ?, ?, ?)",
                       (scope, snapshot_id, cutoff, _json(snapshot)))

        receipt = dict(zip(RECEIPT_FIELDS, (
            edition_id, head["revision"] + 1 if head else 1, parent, *stream, publisher,
            snapshot_id, digest, datetime.now(timezone.utc).isoformat(),
        ), strict=True))
        db.execute(
            "INSERT INTO editions (scope_id, " + ", ".join(RECEIPT_FIELDS) + ", cutoff, payload)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (scope, *receipt.values(), cutoff, raw),
        )
        db.executemany("INSERT INTO candidates VALUES (?, ?, ?, ?, ?)", (
            (scope, edition_id, item["candidate_id"], position, _json(item))
            for position, item in enumerate(payload["report"]["candidates"])
        ))
        return 201, receipt
