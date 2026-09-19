from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any


PROTOCOL_VERSION = "1.0-draft"
ADAPTER_VERSION = "0.1.0"


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def timepoint(value: str | None = None, *, precision: str = "source_exact") -> dict[str, str]:
    if value is None:
        value = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return {"value": value, "precision": precision}


def result_envelope(
    *,
    capability: str,
    producer: str,
    status: str,
    data: Any,
    authority: str,
    generated_at: dict[str, str] | None = None,
    freshness: str = "unknown",
    source_mode: str = "unknown",
    provenance: list[dict[str, Any]] | None = None,
    warnings: list[str] | None = None,
    gaps: list[dict[str, Any]] | None = None,
    conflicts: list[dict[str, Any]] | None = None,
    permissions_used: list[str] | None = None,
    data_times: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "capability": capability,
        "contract_version": PROTOCOL_VERSION,
        "producer": {"id": producer, "version": ADAPTER_VERSION},
        "status": status,
        "data": data,
        "generated_at": generated_at or timepoint(),
        "data_times": data_times or {},
        "freshness": freshness,
        "source_mode": source_mode,
        "provenance": provenance or [],
        "authority": authority,
        "warnings": warnings or [],
        "gaps": gaps or [],
        "conflicts": conflicts or [],
        "permissions_used": sorted(set(permissions_used or [])),
    }


def logicalize_legacy_evidence_refs(value: Any) -> Any:
    """Replace filesystem-shaped compatibility evidence IDs with opaque IDs.

    The low-level projector intentionally records where legacy data came from.
    Client-facing capability results must not turn those paths into protocol
    identity or expose them as part of the stable tool surface.
    """
    if isinstance(value, list):
        return [logicalize_legacy_evidence_refs(item) for item in value]
    if isinstance(value, dict):
        return {key: logicalize_legacy_evidence_refs(item) for key, item in value.items()}
    if isinstance(value, str) and value.startswith("legacy:") and ("/" in value or "\\" in value):
        return f"evidence-ref:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"
    return value


def hide_local_locators(value: Any) -> Any:
    """Remove legacy storage locators from capability-facing data."""
    if isinstance(value, list):
        return [hide_local_locators(item) for item in value]
    if isinstance(value, dict):
        return {key: hide_local_locators(item) for key, item in value.items()}
    if isinstance(value, str):
        normalized = value.replace("\\", "/")
        relative_prefixes = ("accounts/", "knowledge/", "tmp/", ".work/")
        looks_absolute_repo_locator = normalized.startswith("/") and "trademind-framework/" in normalized
        if normalized.startswith(relative_prefixes) or looks_absolute_repo_locator:
            return f"object-ref:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:24]}"
    return value
