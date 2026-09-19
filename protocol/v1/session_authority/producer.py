"""Personal-use native session authority/context producer and exchange publisher."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
from typing import Any, Mapping

from .contracts import (
    CONTRACT_VERSION,
    MARKET,
    SESSION_TIMEZONE,
    SessionAuthorityContractError,
    canonical_json_bytes,
    event_hash_input_bytes,
    replay_authority_payloads,
    sha256_bytes,
    validate_amendment_payload,
    validate_brief_payload,
    validate_material_context_payload,
    validate_utc_timestamp,
)


PRODUCER_ID = "investkitchen"
PRODUCER_RELEASE = "session-authority-v1"
PACKAGE_ID = "trademind-session-exchange"  # compatibility protocol identifier
EXCHANGE_SCHEMA_VERSION = "1.0"

EVENT_FEATURES = {
    "SessionBrief": "session_brief.v1",
    "SessionBriefAmendment": "session_amendment.v1",
    "MaterialContext": "material_context.v1",
}
SCHEMA_BY_EVENT = {
    "SessionBrief": "schemas/session-brief.schema.json",
    "SessionBriefAmendment": "schemas/session-brief-amendment.schema.json",
    "MaterialContext": "schemas/material-context.schema.json",
}
SCHEMA_FILES = (
    "session-contracts.schema.json",
    "session-brief.schema.json",
    "session-brief-amendment.schema.json",
    "material-context.schema.json",
)

# Source locators may be logical identifiers, but raw host paths and secret-like
# markers are not allowed to cross the neutral exchange boundary.
_PRIVATE_MARKERS = (
    b"/Users/",
    b"/home/",
    b"/private/",
    b"/tmp/",
    b"/Volumes/",
    b"/volume1/",
    b"C:\\Users\\",
    b"file://",
    b"accounts/",
    b"api_key",
    b"PRIVATE_KEY",
)

_MANIFEST_FIELDS = {
    "schema_version",
    "event_type",
    "event_id",
    "idempotency_key",
    "producer_id",
    "producer_release",
    "stream_id",
    "sequence",
    "previous_event_hash",
    "event_hash",
    "occurred_at",
    "published_at",
    "effective_at",
    "applicable_session_date",
    "market_timezone",
    "market",
    "correlation_id",
    "causation_id",
    "payload_file",
    "payload_bytes",
    "payload_sha256",
    "required_features",
}


@dataclass(frozen=True)
class AuthorityEvent:
    manifest: dict[str, Any]
    payload: dict[str, Any]
    manifest_bytes: bytes
    payload_bytes: bytes


def _reject_private_payload_bytes(payload_bytes: bytes) -> None:
    for marker in _PRIVATE_MARKERS:
        if marker in payload_bytes:
            raise SessionAuthorityContractError("session payload contains a private path or secret marker")


def _stream_id(event_type: str, session_date: str) -> str:
    return f"{'context' if event_type == 'MaterialContext' else 'authority'}:{session_date}"


def _event_identity(event_type: str, session_date: str, payload_bytes: bytes) -> tuple[str, str]:
    stream_id = _stream_id(event_type, session_date)
    payload_hash = sha256_bytes(payload_bytes)
    idempotency_key = f"{stream_id}:{event_type}:{session_date}:{payload_hash}"
    event_id = "session-" + hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:24]
    return idempotency_key, event_id


def _make_event(
    *,
    event_type: str,
    session_date: str,
    payload: Mapping[str, Any],
    sequence: int,
    previous_event_hash: str | None,
    occurred_at: str,
    effective_at: str | None,
    causation_id: str | None,
) -> AuthorityEvent:
    if event_type == "SessionBrief":
        checked_payload = validate_brief_payload(payload, expected_session_date=session_date)
    elif event_type == "SessionBriefAmendment":
        checked_payload = validate_amendment_payload(payload, expected_session_date=session_date)
    elif event_type == "MaterialContext":
        checked_payload = validate_material_context_payload(payload, expected_session_date=session_date)
    else:  # pragma: no cover - internal caller has a fixed event set
        raise SessionAuthorityContractError("unsupported session event type")
    validate_utc_timestamp(occurred_at, label="occurred_at")
    effective = effective_at or occurred_at
    validate_utc_timestamp(effective, label="effective_at")
    payload_bytes = canonical_json_bytes(checked_payload)
    _reject_private_payload_bytes(payload_bytes)
    payload_hash = sha256_bytes(payload_bytes)
    idempotency_key, event_id = _event_identity(event_type, session_date, payload_bytes)
    manifest_without_hash: dict[str, Any] = {
        "schema_version": CONTRACT_VERSION,
        "event_type": event_type,
        "event_id": event_id,
        "idempotency_key": idempotency_key,
        "producer_id": PRODUCER_ID,
        "producer_release": PRODUCER_RELEASE,
        "stream_id": _stream_id(event_type, session_date),
        "sequence": sequence,
        "previous_event_hash": previous_event_hash,
        "occurred_at": occurred_at,
        "published_at": occurred_at,
        "effective_at": effective,
        "applicable_session_date": session_date,
        "market_timezone": SESSION_TIMEZONE,
        "market": MARKET,
        "correlation_id": f"session:{session_date}",
        "causation_id": causation_id,
        "payload_file": "payload.json",
        "payload_bytes": len(payload_bytes),
        "payload_sha256": payload_hash,
        "required_features": [EVENT_FEATURES[event_type]],
    }
    manifest = dict(manifest_without_hash)
    manifest["event_hash"] = sha256_bytes(event_hash_input_bytes(manifest_without_hash, payload_bytes))
    manifest_bytes = canonical_json_bytes(manifest)
    event = AuthorityEvent(manifest, checked_payload, manifest_bytes, payload_bytes)
    _validate_event(event)
    return event


def _validate_event(event: AuthorityEvent) -> None:
    manifest = event.manifest
    if set(manifest) != _MANIFEST_FIELDS:
        raise SessionAuthorityContractError("session authority manifest fields are invalid")
    event_type = manifest["event_type"]
    if event_type not in EVENT_FEATURES:
        raise SessionAuthorityContractError("session authority event type is invalid")
    if manifest["schema_version"] != CONTRACT_VERSION:
        raise SessionAuthorityContractError("session authority schema version is invalid")
    if manifest["producer_id"] != PRODUCER_ID or manifest["producer_release"] != PRODUCER_RELEASE:
        raise SessionAuthorityContractError("session authority producer identity is invalid")
    session_date = manifest["applicable_session_date"]
    if manifest["stream_id"] != _stream_id(event_type, session_date) or manifest["market_timezone"] != SESSION_TIMEZONE or manifest["market"] != MARKET:
        raise SessionAuthorityContractError("session stream/market identity is invalid")
    if manifest["payload_file"] != "payload.json" or manifest["required_features"] != [EVENT_FEATURES[event_type]]:
        raise SessionAuthorityContractError("session authority manifest feature identity is invalid")
    if not isinstance(manifest["sequence"], int) or isinstance(manifest["sequence"], bool) or manifest["sequence"] < 1:
        raise SessionAuthorityContractError("session authority sequence is invalid")
    for field in ("occurred_at", "published_at", "effective_at"):
        validate_utc_timestamp(manifest[field], label=field)
    if event.manifest_bytes != canonical_json_bytes(manifest):
        raise SessionAuthorityContractError("session authority manifest bytes are not canonical")
    if len(event.payload_bytes) != manifest["payload_bytes"] or sha256_bytes(event.payload_bytes) != manifest["payload_sha256"]:
        raise SessionAuthorityContractError("session authority payload bytes/hash do not match manifest")
    expected_idempotency, expected_event_id = _event_identity(event_type, session_date, event.payload_bytes)
    if manifest["idempotency_key"] != expected_idempotency or manifest["event_id"] != expected_event_id:
        raise SessionAuthorityContractError("session authority event identity is not deterministic")
    identity = dict(manifest)
    event_hash = identity.pop("event_hash")
    if sha256_bytes(event_hash_input_bytes(identity, event.payload_bytes)) != event_hash:
        raise SessionAuthorityContractError("session authority event hash is invalid")
    if event_type == "SessionBrief":
        validate_brief_payload(event.payload, expected_session_date=session_date)
    elif event_type == "SessionBriefAmendment":
        validate_amendment_payload(event.payload, expected_session_date=session_date)
    else:
        validate_material_context_payload(event.payload, expected_session_date=session_date)
    if canonical_json_bytes(event.payload) != event.payload_bytes:
        raise SessionAuthorityContractError("session authority payload bytes are not canonical")
    _reject_private_payload_bytes(event.payload_bytes)


def _validate_chain(events: list[AuthorityEvent], session_date: str) -> None:
    if not events or events[0].manifest["event_type"] != "SessionBrief":
        raise SessionAuthorityContractError("authority stream must begin with SessionBrief")
    previous: AuthorityEvent | None = None
    base = events[0]
    amendments: list[Mapping[str, Any]] = []
    seen_event_ids: set[str] = set()
    seen_idempotency: set[str] = set()
    for expected_sequence, event in enumerate(events, start=1):
        _validate_event(event)
        manifest = event.manifest
        if manifest["applicable_session_date"] != session_date or manifest["sequence"] != expected_sequence:
            raise SessionAuthorityContractError("authority stream sequence/session identity is invalid")
        expected_previous = None if previous is None else previous.manifest["event_hash"]
        expected_causation = None if previous is None else previous.manifest["event_id"]
        if manifest["previous_event_hash"] != expected_previous or manifest["causation_id"] != expected_causation:
            raise SessionAuthorityContractError("authority predecessor linkage is invalid")
        if manifest["event_id"] in seen_event_ids or manifest["idempotency_key"] in seen_idempotency:
            raise SessionAuthorityContractError("authority event identity is duplicated")
        seen_event_ids.add(manifest["event_id"])
        seen_idempotency.add(manifest["idempotency_key"])
        if expected_sequence == 1:
            if manifest["event_type"] != "SessionBrief":
                raise SessionAuthorityContractError("authority stream first event must be SessionBrief")
        else:
            if manifest["event_type"] != "SessionBriefAmendment":
                raise SessionAuthorityContractError("authority stream contains an unsupported event")
            payload = event.payload
            if payload["base_brief_id"] != base.manifest["event_id"] or payload["base_brief_hash"] != base.manifest["event_hash"]:
                raise SessionAuthorityContractError("amendment base brief identity is invalid")
            assert previous is not None
            if payload["previous_authority_event"] != {
                "event_id": previous.manifest["event_id"],
                "event_hash": previous.manifest["event_hash"],
            }:
                raise SessionAuthorityContractError("amendment previous authority identity is invalid")
            amendments.append(payload)
        previous = event
    replay_authority_payloads(base.payload, amendments)


def _validate_context_chain(
    events: list[AuthorityEvent],
    session_date: str,
    *,
    authority_event_ids: set[str],
) -> None:
    previous: AuthorityEvent | None = None
    seen_event_ids: set[str] = set()
    seen_idempotency: set[str] = set()
    for expected_sequence, event in enumerate(events, start=1):
        _validate_event(event)
        manifest = event.manifest
        if manifest["event_type"] != "MaterialContext":
            raise SessionAuthorityContractError("context stream contains an unsupported event")
        if manifest["applicable_session_date"] != session_date or manifest["sequence"] != expected_sequence:
            raise SessionAuthorityContractError("context stream sequence/session identity is invalid")
        expected_previous = None if previous is None else previous.manifest["event_hash"]
        if manifest["previous_event_hash"] != expected_previous:
            raise SessionAuthorityContractError("context predecessor linkage is invalid")
        if manifest["causation_id"] not in authority_event_ids:
            raise SessionAuthorityContractError("MaterialContext causation must bind current-session authority")
        if manifest["event_id"] in seen_event_ids or manifest["idempotency_key"] in seen_idempotency:
            raise SessionAuthorityContractError("context event identity is duplicated")
        seen_event_ids.add(manifest["event_id"])
        seen_idempotency.add(manifest["idempotency_key"])
        previous = event


def _streams_are_append_only(
    existing_events: list[dict[str, Any]],
    proposed_events: list[dict[str, Any]],
) -> bool:
    old_by_stream: dict[str, list[dict[str, Any]]] = {}
    new_by_stream: dict[str, list[dict[str, Any]]] = {}
    for event in existing_events:
        stream_id = event.get("stream_id")
        if not isinstance(stream_id, str):
            return False
        old_by_stream.setdefault(stream_id, []).append(event)
    for event in proposed_events:
        stream_id = event.get("stream_id")
        if not isinstance(stream_id, str):
            return False
        new_by_stream.setdefault(stream_id, []).append(event)
    return all(
        len(old_items) <= len(new_by_stream.get(stream_id, []))
        and new_by_stream[stream_id][: len(old_items)] == old_items
        for stream_id, old_items in old_by_stream.items()
    )


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        view = memoryview(content)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("session authority exchange write failed")
            view = view[written:]
        os.fsync(fd)
    finally:
        os.close(fd)


def _atomic_replace_bytes(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    if temporary.exists() or temporary.is_symlink():
        raise SessionAuthorityContractError("exchange metadata staging path already exists")
    _write_bytes(temporary, content)
    try:
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        if temporary.exists() and not temporary.is_symlink():
            temporary.unlink()


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SessionAuthorityContractError(f"exchange metadata is unreadable: {path.name}") from exc
    if not isinstance(value, dict):
        raise SessionAuthorityContractError(f"exchange metadata is invalid: {path.name}")
    return value


class SessionAuthorityProducer:
    """Build and publish one date-scoped immutable authority chain."""

    def __init__(self, session_date: str) -> None:
        try:
            parsed = date_from_iso(session_date)
        except ValueError as exc:
            raise SessionAuthorityContractError("session_date must be YYYY-MM-DD") from exc
        self.session_date = parsed
        self._events: list[AuthorityEvent] = []
        self._context_events: list[AuthorityEvent] = []

    @property
    def events(self) -> tuple[AuthorityEvent, ...]:
        """Return the authority stream for backward-compatible callers."""
        return tuple(self._events)

    @property
    def context_events(self) -> tuple[AuthorityEvent, ...]:
        return tuple(self._context_events)

    @property
    def all_events(self) -> tuple[AuthorityEvent, ...]:
        return tuple([*self._events, *self._context_events])

    def append_brief(
        self,
        payload: Mapping[str, Any],
        *,
        occurred_at: str,
        effective_at: str | None = None,
    ) -> AuthorityEvent:
        if self._events:
            raise SessionAuthorityContractError("SessionBrief can only initialize an empty authority stream")
        event = _make_event(
            event_type="SessionBrief",
            session_date=self.session_date,
            payload=payload,
            sequence=1,
            previous_event_hash=None,
            occurred_at=occurred_at,
            effective_at=effective_at,
            causation_id=None,
        )
        _validate_chain([event], self.session_date)
        self._events.append(event)
        return event

    def append_amendment(
        self,
        payload: Mapping[str, Any],
        *,
        occurred_at: str,
        effective_at: str | None = None,
    ) -> AuthorityEvent:
        if not self._events:
            raise SessionAuthorityContractError("SessionBriefAmendment requires an existing SessionBrief")
        previous = self._events[-1]
        event = _make_event(
            event_type="SessionBriefAmendment",
            session_date=self.session_date,
            payload=payload,
            sequence=len(self._events) + 1,
            previous_event_hash=previous.manifest["event_hash"],
            occurred_at=occurred_at,
            effective_at=effective_at,
            causation_id=previous.manifest["event_id"],
        )
        candidate_chain = [*self._events, event]
        _validate_chain(candidate_chain, self.session_date)
        self._events.append(event)
        return event

    def append_context(
        self,
        payload: Mapping[str, Any],
        *,
        occurred_at: str,
        effective_at: str | None = None,
    ) -> AuthorityEvent:
        """Append non-authoritative supporting material to ``context:<date>``."""

        if not self._events:
            raise SessionAuthorityContractError("MaterialContext requires an existing SessionBrief")
        previous = self._context_events[-1] if self._context_events else None
        authority_head = self._events[-1]
        event = _make_event(
            event_type="MaterialContext",
            session_date=self.session_date,
            payload=payload,
            sequence=len(self._context_events) + 1,
            previous_event_hash=None if previous is None else previous.manifest["event_hash"],
            occurred_at=occurred_at,
            effective_at=effective_at,
            causation_id=authority_head.manifest["event_id"],
        )
        candidate_chain = [*self._context_events, event]
        _validate_context_chain(
            candidate_chain,
            self.session_date,
            authority_event_ids={item.manifest["event_id"] for item in self._events},
        )
        self._context_events.append(event)
        return event

    def authority_state(self) -> dict[str, Any]:
        if not self._events:
            raise SessionAuthorityContractError("session authority stream is empty")
        _validate_chain(self._events, self.session_date)
        records, statuses, source_ids = replay_authority_payloads(
            self._events[0].payload,
            [event.payload for event in self._events[1:]],
        )
        return {
            "session_date": self.session_date,
            "event_id": self._events[-1].manifest["event_id"],
            "event_hash": self._events[-1].manifest["event_hash"],
            "records": {key: dict(value) for key, value in sorted(records.items())},
            "statuses": dict(sorted(statuses.items())),
            "authorized_document_ids": sorted(source_ids),
        }

    def _exchange_plan(self, *, published_at: str) -> tuple[dict[str, bytes], dict[str, Any], dict[str, Any]]:
        if not self._events:
            raise SessionAuthorityContractError("cannot publish an empty session authority stream")
        validate_utc_timestamp(published_at, label="exchange published_at")
        _validate_chain(self._events, self.session_date)
        _validate_context_chain(
            self._context_events,
            self.session_date,
            authority_event_ids={item.manifest["event_id"] for item in self._events},
        )
        module_dir = Path(__file__).resolve().parent
        files: dict[str, bytes] = {}
        artifacts: list[dict[str, Any]] = []
        index_events: list[dict[str, Any]] = []

        def add(relative: str, content: bytes, kind: str) -> None:
            files[relative] = content
            artifacts.append({"path": relative, "kind": kind, "sha256": sha256_bytes(content), "bytes": len(content)})

        for schema_name in SCHEMA_FILES:
            schema_path = module_dir / "schemas" / schema_name
            try:
                content = schema_path.read_bytes()
            except OSError as exc:
                raise SessionAuthorityContractError(f"session authority schema is unavailable: {schema_name}") from exc
            add(f"schemas/{schema_name}", content, "schema")

        authorized_document_ids: set[str] = set()
        for event in [*self._events, *self._context_events]:
            if event.manifest["event_type"] == "SessionBrief":
                authorized_document_ids.update(str(item["document_id"]) for item in event.payload["source_documents"])
            elif event.manifest["event_type"] == "SessionBriefAmendment":
                authorized_document_ids.add(str(event.payload["source"]["document_id"]))
            sequence = int(event.manifest["sequence"])
            event_type = str(event.manifest["event_type"])
            stream_label = "context" if event_type == "MaterialContext" else "authority"
            directory = f"events/{stream_label}-{self.session_date}/{sequence:04d}-{event_type}"
            manifest_path = f"{directory}/manifest.json"
            payload_path = f"{directory}/payload.json"
            add(manifest_path, event.manifest_bytes, "manifest")
            add(payload_path, event.payload_bytes, "payload")
            index_events.append({
                "event_type": event_type,
                "event_id": event.manifest["event_id"],
                "event_hash": event.manifest["event_hash"],
                "stream_id": event.manifest["stream_id"],
                "sequence": sequence,
                "manifest_path": manifest_path,
                "payload_path": payload_path,
                "schema_path": SCHEMA_BY_EVENT[event_type],
                "manifest_sha256": sha256_bytes(event.manifest_bytes),
                "manifest_bytes": len(event.manifest_bytes),
                "payload_sha256": sha256_bytes(event.payload_bytes),
                "payload_bytes": len(event.payload_bytes),
            })
        artifacts.sort(key=lambda item: item["path"])
        index_events.sort(key=lambda item: (item["stream_id"], item["sequence"], item["event_type"]))
        common = {
            "package_id": PACKAGE_ID,
            "exchange_schema_version": EXCHANGE_SCHEMA_VERSION,
            "contract_schema_version": CONTRACT_VERSION,
            "session_date": self.session_date,
            "producer_id": PRODUCER_ID,
            "producer_release": PRODUCER_RELEASE,
            "required_features": sorted(EVENT_FEATURES.values()),
            "authorized_document_ids": sorted(authorized_document_ids),
            "artifacts": artifacts,
            "events": index_events,
        }
        index = dict(common)
        descriptor = {
            **common,
            "publisher": "investkitchen_session_authority",
            "generation_command": "programmatic:protocol.v1.session_authority.SessionAuthorityProducer",
            "published_at": published_at,
            "index_path": "index.json",
        }
        return files, index, descriptor

    def publish_exchange(self, exchange_root: Path, *, published_at: str) -> dict[str, Any]:
        """Atomically publish or append this chain under ``exchange_root/<date>``.

        Existing event bytes must be an exact prefix of the proposed authority
        chain. They are never overwritten. The returned result deliberately does
        not expose an absolute host path.
        """

        files, index, descriptor = self._exchange_plan(published_at=published_at)
        root = exchange_root.expanduser()
        if root.is_symlink():
            raise SessionAuthorityContractError("exchange root must not be a symlink")
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        target = root / self.session_date
        if target.is_symlink():
            raise SessionAuthorityContractError("exchange session directory must not be a symlink")
        fingerprint = sha256_bytes(canonical_json_bytes({"events": index["events"], "artifacts": index["artifacts"]}))[:16]
        staging = root / f".staging-{self.session_date}-{fingerprint}"
        lock_path = root / f".{self.session_date}.publish.lock"
        if staging.exists() or staging.is_symlink() or lock_path.is_symlink():
            raise SessionAuthorityContractError("exchange staging/lock path is unsafe")
        lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(lock_fd, "r+") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                existing_events: list[dict[str, Any]] = []
                old_artifacts: dict[str, dict[str, Any]] = {}
                if target.exists():
                    if not target.is_dir():
                        raise SessionAuthorityContractError("exchange session target is not a directory")
                    current_index = _load_json(target / "index.json")
                    if (
                        current_index.get("package_id") != PACKAGE_ID
                        or current_index.get("producer_id") != PRODUCER_ID
                        or current_index.get("producer_release") != PRODUCER_RELEASE
                        or current_index.get("session_date") != self.session_date
                    ):
                        raise SessionAuthorityContractError("existing exchange producer/session identity is incompatible")
                    current_descriptor = _load_json(target / "exchange.json")
                    if (
                        current_descriptor.get("package_id") != PACKAGE_ID
                        or current_descriptor.get("producer_id") != PRODUCER_ID
                        or current_descriptor.get("producer_release") != PRODUCER_RELEASE
                        or current_descriptor.get("session_date") != self.session_date
                        or current_descriptor.get("events") != current_index.get("events")
                        or current_descriptor.get("artifacts") != current_index.get("artifacts")
                    ):
                        raise SessionAuthorityContractError("existing exchange descriptor disagrees with index")
                    raw_events = current_index.get("events")
                    raw_artifacts = current_index.get("artifacts")
                    if not isinstance(raw_events, list) or not isinstance(raw_artifacts, list):
                        raise SessionAuthorityContractError("existing exchange inventory is invalid")
                    existing_events = [dict(item) for item in raw_events if isinstance(item, Mapping)]
                    if len(existing_events) != len(raw_events) or not _streams_are_append_only(existing_events, index["events"]):
                        raise SessionAuthorityContractError("existing exchange is not an exact per-stream prefix")
                    for item in raw_artifacts:
                        if not isinstance(item, Mapping) or not isinstance(item.get("path"), str):
                            raise SessionAuthorityContractError("existing exchange artifact inventory is invalid")
                        relative = Path(str(item["path"]))
                        if relative.is_absolute() or ".." in relative.parts:
                            raise SessionAuthorityContractError("existing exchange artifact path is unsafe")
                        path = target / relative
                        if path.is_symlink() or not path.is_file():
                            raise SessionAuthorityContractError("existing exchange artifact is missing")
                        content = path.read_bytes()
                        if item.get("sha256") != sha256_bytes(content) or item.get("bytes") != len(content):
                            raise SessionAuthorityContractError("existing exchange artifact integrity failed")
                        old_artifacts[str(item["path"])] = dict(item)
                    proposed_artifacts = {item["path"]: item for item in index["artifacts"]}
                    for path, item in old_artifacts.items():
                        if proposed_artifacts.get(path) != item:
                            raise SessionAuthorityContractError("existing exchange artifact would be mutated")
                    if existing_events == index["events"] and len(old_artifacts) == len(index["artifacts"]):
                        return {
                            "ok": True,
                            "status": "already_published",
                            "session_date": self.session_date,
                            "event_count": len(index["events"]),
                            "last_event_id": index["events"][-1]["event_id"],
                        }

                staging.mkdir(mode=0o700)
                try:
                    for relative, content in sorted(files.items()):
                        if relative in old_artifacts:
                            continue
                        _write_bytes(staging / relative, content)
                    _write_bytes(staging / "index.json", canonical_json_bytes(index))
                    _write_bytes(staging / "exchange.json", canonical_json_bytes(descriptor))
                    _fsync_dir(staging)
                    if not target.exists():
                        os.replace(staging, target)
                    else:
                        # Schemas remain immutable. Only new event directories are
                        # moved in before the two metadata files are atomically replaced.
                        new_event_dirs = sorted(
                            {
                                Path(relative).parent
                                for relative in files
                                if relative not in old_artifacts and relative.startswith("events/")
                            },
                            key=lambda item: item.as_posix(),
                        )
                        for relative_dir in new_event_dirs:
                            source = staging / relative_dir
                            destination = target / relative_dir
                            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                            if destination.exists() or destination.is_symlink():
                                raise SessionAuthorityContractError("new exchange event directory already exists")
                            os.replace(source, destination)
                        # A future producer release may add a schema, but it may
                        # never alter an already published schema artifact.
                        for relative in sorted(files):
                            if not relative.startswith("schemas/") or relative in old_artifacts:
                                continue
                            source = staging / relative
                            destination = target / relative
                            destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                            if destination.exists() or destination.is_symlink():
                                raise SessionAuthorityContractError("new exchange schema path already exists")
                            os.replace(source, destination)
                        _atomic_replace_bytes(target / "index.json", (staging / "index.json").read_bytes())
                        _atomic_replace_bytes(target / "exchange.json", (staging / "exchange.json").read_bytes())
                        shutil.rmtree(staging)
                    _fsync_dir(target)
                    _fsync_dir(root)
                except Exception:
                    if staging.exists() and not staging.is_symlink():
                        shutil.rmtree(staging, ignore_errors=True)
                    raise
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        return {
            "ok": True,
            "status": "published",
            "session_date": self.session_date,
            "event_count": len(index["events"]),
            "last_event_id": index["events"][-1]["event_id"],
        }


def date_from_iso(value: str) -> str:
    return date.fromisoformat(value).isoformat()


__all__ = [
    "AuthorityEvent",
    "EXCHANGE_SCHEMA_VERSION",
    "PACKAGE_ID",
    "PRODUCER_ID",
    "PRODUCER_RELEASE",
    "SessionAuthorityProducer",
]
