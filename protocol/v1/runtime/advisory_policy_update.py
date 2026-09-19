from __future__ import annotations

import copy
from datetime import datetime, timezone
from typing import Any, Mapping

from protocol.v1.adapters.common import PROTOCOL_VERSION, digest, timepoint
from protocol.v1.runtime.advisory_state_store import AdvisoryPolicyStore


class AdvisoryPolicyUpdateError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _deep_merge(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(base))
    for key, value in patch.items():
        if value is None:
            raise AdvisoryPolicyUpdateError("policy_patch_deletion_not_allowed")
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _utc_point(now: datetime | None = None) -> dict[str, str]:
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    return timepoint(current.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"))


def build_policy_update_preview(
    store: AdvisoryPolicyStore,
    request: Mapping[str, Any],
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    allowed = {"portfolio_id", "rules_patch", "source_ref", "note", "scope"}
    if not isinstance(request, Mapping) or set(request) - allowed:
        raise AdvisoryPolicyUpdateError("policy_update_request_invalid")
    portfolio_id = str(request.get("portfolio_id") or "").strip()
    source_ref = str(request.get("source_ref") or "").strip()
    rules_patch = request.get("rules_patch")
    if not portfolio_id or not source_ref or not isinstance(rules_patch, Mapping) or not rules_patch:
        raise AdvisoryPolicyUpdateError("policy_update_request_invalid")
    current = store.current(portfolio_id)
    if current is None:
        raise AdvisoryPolicyUpdateError("policy_current_missing")
    if current.get("order_authorized") is not False:
        raise AdvisoryPolicyUpdateError("policy_execution_authority_invalid")

    merged_rules = _deep_merge(current.get("rules") or {}, rules_patch)
    if merged_rules == current.get("rules"):
        raise AdvisoryPolicyUpdateError("policy_update_no_effect")
    effective = _utc_point(now)
    material = {
        "portfolio_id": portfolio_id,
        "rules": merged_rules,
        "supersedes": current["policy_id"],
        "effective_from": effective,
        "source_ref": source_ref,
    }
    revision = digest(material)
    version = "native-" + revision[:16]
    policy_id = f"policy:{portfolio_id}:{version}"
    next_policy = {
        **copy.deepcopy(current),
        "protocol_version": PROTOCOL_VERSION,
        "policy_id": policy_id,
        "portfolio_id": portfolio_id,
        "scope": str(request.get("scope") or "portfolio_advisory"),
        "status": "active",
        "effective_from": effective,
        "authority": "user_policy_record",
        "order_authorized": False,
        "rules": merged_rules,
        "supersedes": current["policy_id"],
        "version": version,
        "source_evidence": list(dict.fromkeys([
            *[str(value) for value in current.get("source_evidence") or [] if str(value)],
            source_ref,
        ])),
    }
    note = request.get("note")
    notes = [str(value) for value in current.get("data_quality_notes") or []]
    if isinstance(note, str) and note.strip():
        notes.append(note.strip())
    next_policy["data_quality_notes"] = notes
    base_digest = store.revision_digest()
    candidate_material = {
        "base_digest": base_digest,
        "current_policy_id": current["policy_id"],
        "next_policy": next_policy,
    }
    candidate_digest = digest(candidate_material)
    return {
        "status": "review_required",
        "preview_id": "policy-preview:" + candidate_digest[:24],
        "candidate_digest": candidate_digest,
        "base_digest": base_digest,
        "portfolio_id": portfolio_id,
        "current_policy_id": current["policy_id"],
        "next_policy": next_policy,
        "changed_rule_keys": sorted(str(key) for key in rules_patch),
        "confirmation": f"APPROVE POLICY UPDATE {candidate_digest[:12]}",
    }


def apply_policy_update(
    store: AdvisoryPolicyStore,
    preview: Mapping[str, Any],
) -> dict[str, Any]:
    if not isinstance(preview, Mapping) or preview.get("status") != "review_required":
        raise AdvisoryPolicyUpdateError("policy_preview_invalid")
    portfolio_id = str(preview.get("portfolio_id") or "")
    base_digest = str(preview.get("base_digest") or "")
    next_policy = preview.get("next_policy")
    if not portfolio_id or not base_digest or not isinstance(next_policy, dict):
        raise AdvisoryPolicyUpdateError("policy_preview_invalid")
    current = store.current(portfolio_id)
    if current is None or current.get("policy_id") != preview.get("current_policy_id"):
        raise AdvisoryPolicyUpdateError("policy_preview_stale")
    if store.revision_digest() != base_digest:
        raise AdvisoryPolicyUpdateError("policy_preview_stale")

    rows = store.list_policies()
    replaced = False
    for row in rows:
        if row.get("portfolio_id") == portfolio_id and row.get("status") == "active":
            row["status"] = "superseded"
            replaced = True
    if not replaced:
        raise AdvisoryPolicyUpdateError("policy_current_missing")
    rows.append(copy.deepcopy(next_policy))
    applied_at = _utc_point()
    new_digest = store.replace_policies(
        rows,
        expected_digest=base_digest,
        mutation={
            "kind": "policy_update",
            "portfolio_id": portfolio_id,
            "policy_id": next_policy["policy_id"],
            "supersedes": preview.get("current_policy_id"),
            "applied_at": applied_at,
        },
    )
    read_back = store.current(portfolio_id)
    if read_back is None or read_back.get("policy_id") != next_policy.get("policy_id"):
        raise AdvisoryPolicyUpdateError("policy_readback_mismatch")
    return {
        "status": "applied",
        "portfolio_id": portfolio_id,
        "policy_id": read_back["policy_id"],
        "supersedes": read_back.get("supersedes"),
        "store_digest": new_digest,
        "applied_at": applied_at,
        "read_back": {"matches": True, "rules_digest": digest(read_back.get("rules") or {})},
    }


__all__ = [
    "AdvisoryPolicyUpdateError",
    "apply_policy_update",
    "build_policy_update_preview",
]
