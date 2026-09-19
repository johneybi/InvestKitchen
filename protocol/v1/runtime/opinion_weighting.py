from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from protocol.v1.adapters.common import canonical_json, digest, result_envelope, timepoint


class OpinionWeightingError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _private_write(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    encoded = (canonical_json(value) + "\n").encode("utf-8")
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.write(fd, encoded)
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(temp, path)


def _float(value: Any, *, code: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OpinionWeightingError(code)
    result = float(value)
    if result < 0:
        raise OpinionWeightingError(code)
    return result


def validate_opinion_weighting_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict) or str(config.get("policy_version") or "") == "":
        raise OpinionWeightingError("opinion_weighting_config_invalid")
    modes = config.get("mode_policies")
    if not isinstance(modes, dict) or set(modes) != {"short_term", "medium_term", "long_term"}:
        raise OpinionWeightingError("opinion_weighting_modes_invalid")
    for mode_name, mode in modes.items():
        if not isinstance(mode, dict) or not isinstance(mode.get("anchor_weights"), dict):
            raise OpinionWeightingError("opinion_weighting_mode_invalid")
        anchors = mode["anchor_weights"]
        if not anchors or any(not isinstance(speaker, str) or not speaker.strip() for speaker in anchors):
            raise OpinionWeightingError("opinion_weighting_anchor_invalid")
        anchor_total = sum(_float(value, code="opinion_weighting_anchor_invalid") for value in anchors.values())
        supplemental = _float(mode.get("supplemental_pool"), code="opinion_weighting_pool_invalid")
        reserve = _float(mode.get("base_reserve"), code="opinion_weighting_reserve_invalid")
        if abs(anchor_total + supplemental + reserve - 1.0) > 1e-6:
            raise OpinionWeightingError("opinion_weighting_budget_invalid")
        if any(float(value) > 1 for value in anchors.values()):
            raise OpinionWeightingError("opinion_weighting_anchor_invalid")
        horizons = mode.get("horizons")
        if not isinstance(horizons, list) or any(not isinstance(value, str) for value in horizons):
            raise OpinionWeightingError("opinion_weighting_horizons_invalid")
    prefs = config.get("user_preferences")
    if not isinstance(prefs, dict):
        raise OpinionWeightingError("opinion_weighting_preferences_invalid")
    for field in ("speaker_multipliers", "speaker_approach_multipliers"):
        value = prefs.get(field, {})
        if not isinstance(value, dict):
            raise OpinionWeightingError("opinion_weighting_preferences_invalid")
    excluded = prefs.get("excluded_speakers", [])
    if not isinstance(excluded, list) or any(not isinstance(value, str) for value in excluded):
        raise OpinionWeightingError("opinion_weighting_preferences_invalid")
    return config


class OpinionWeightingStore:
    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.config_path = self.root / "config.json"
        self.manifest_path = self.root / "manifest.json"
        self.lock_path = self.root / ".opinion-weighting.lock"

    @contextmanager
    def _locked(self, *, exclusive: bool):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        with os.fdopen(fd, "r+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def exists(self) -> bool:
        return self.config_path.is_file() and self.manifest_path.is_file()

    def validate(self) -> dict[str, Any]:
        with self._locked(exclusive=False):
            config = json.loads(self.config_path.read_text(encoding="utf-8"))
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            validate_opinion_weighting_config(config)
            if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
                raise OpinionWeightingError("opinion_weighting_manifest_invalid")
            if manifest.get("config_sha256") != _sha256_file(self.config_path):
                raise OpinionWeightingError("opinion_weighting_digest_mismatch")
            if manifest.get("policy_version") != config.get("policy_version"):
                raise OpinionWeightingError("opinion_weighting_version_mismatch")
            return manifest

    def read(self) -> dict[str, Any]:
        self.validate()
        with self._locked(exclusive=False):
            return json.loads(self.config_path.read_text(encoding="utf-8"))

    def revision_digest(self) -> str:
        self.validate()
        return _sha256_file(self.config_path)

    def initialize(self, config: Mapping[str, Any], *, source_ref: str) -> None:
        value = copy.deepcopy(dict(config))
        validate_opinion_weighting_config(value)
        with self._locked(exclusive=True):
            if self.exists():
                raise OpinionWeightingError("opinion_weighting_already_initialized")
            _private_write(self.config_path, value)
            _private_write(self.manifest_path, {
                "schema_version": 1,
                "policy_version": value["policy_version"],
                "source_ref": source_ref,
                "config_sha256": _sha256_file(self.config_path),
                "mutation_history": [],
            })
        self.validate()

    def replace(self, config: Mapping[str, Any], *, expected_digest: str, mutation: Mapping[str, Any]) -> str:
        value = copy.deepcopy(dict(config))
        validate_opinion_weighting_config(value)
        with self._locked(exclusive=True):
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
            if _sha256_file(self.config_path) != expected_digest:
                raise OpinionWeightingError("opinion_weighting_base_version_conflict")
            _private_write(self.config_path, value)
            new_digest = _sha256_file(self.config_path)
            history = list(manifest.get("mutation_history") or [])
            history.append(copy.deepcopy(dict(mutation)))
            manifest.update({
                "policy_version": value["policy_version"],
                "config_sha256": new_digest,
                "mutation_history": history[-100:],
            })
            _private_write(self.manifest_path, manifest)
        self.validate()
        return new_digest


def _deep_merge(base: Mapping[str, Any], patch: Mapping[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(dict(base))
    for key, value in patch.items():
        if value is None:
            raise OpinionWeightingError("opinion_weighting_deletion_not_allowed")
        if isinstance(value, Mapping) and isinstance(result.get(key), Mapping):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def build_weighting_update_preview(store: OpinionWeightingStore, request: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {"changes", "source_ref", "note"}
    if not isinstance(request, Mapping) or set(request) - allowed:
        raise OpinionWeightingError("opinion_weighting_update_request_invalid")
    changes = request.get("changes")
    source_ref = str(request.get("source_ref") or "").strip()
    if not isinstance(changes, Mapping) or not changes or not source_ref:
        raise OpinionWeightingError("opinion_weighting_update_request_invalid")
    if set(changes) - {"mode_policies", "user_preferences"}:
        raise OpinionWeightingError("opinion_weighting_update_scope_invalid")
    current = store.read()
    proposed = _deep_merge(current, changes)
    if proposed == current:
        raise OpinionWeightingError("opinion_weighting_update_no_effect")
    base_digest = store.revision_digest()
    revision_material = {"base_digest": base_digest, "changes": changes, "source_ref": source_ref}
    candidate_digest = digest(revision_material)
    effective = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    proposed["policy_version"] = "native-" + candidate_digest[:16]
    history = list(proposed.get("revision_history") or [])
    history.append({
        "version": proposed["policy_version"],
        "effective_at": effective,
        "source_type": "user_statement",
        "change": str(request.get("note") or "User-updated opinion weighting policy."),
        "reason": source_ref,
    })
    proposed["revision_history"] = history
    validate_opinion_weighting_config(proposed)
    return {
        "status": "review_required",
        "preview_id": "opinion-weighting-preview:" + candidate_digest[:24],
        "candidate_digest": candidate_digest,
        "base_digest": base_digest,
        "current_policy_version": current["policy_version"],
        "next_policy_version": proposed["policy_version"],
        "proposed_config": proposed,
        "changed_sections": sorted(str(key) for key in changes),
        "confirmation": f"APPROVE OPINION WEIGHTING UPDATE {candidate_digest[:12]}",
    }


def apply_weighting_update(store: OpinionWeightingStore, preview: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(preview, Mapping) or preview.get("status") != "review_required":
        raise OpinionWeightingError("opinion_weighting_preview_invalid")
    base_digest = str(preview.get("base_digest") or "")
    proposed = preview.get("proposed_config")
    if not base_digest or not isinstance(proposed, Mapping):
        raise OpinionWeightingError("opinion_weighting_preview_invalid")
    if store.revision_digest() != base_digest:
        raise OpinionWeightingError("opinion_weighting_preview_stale")
    applied_at = timepoint()
    new_digest = store.replace(
        proposed,
        expected_digest=base_digest,
        mutation={
            "kind": "opinion_weighting_update",
            "policy_version": proposed["policy_version"],
            "applied_at": applied_at,
        },
    )
    read_back = store.read()
    if read_back.get("policy_version") != proposed.get("policy_version"):
        raise OpinionWeightingError("opinion_weighting_readback_mismatch")
    return {
        "status": "applied",
        "policy_version": read_back["policy_version"],
        "store_digest": new_digest,
        "applied_at": applied_at,
        "read_back": {"matches": True, "config_digest": digest(read_back)},
    }


def _canonical_speaker(config: Mapping[str, Any], speaker: str) -> str:
    aliases = config.get("speaker_aliases") if isinstance(config.get("speaker_aliases"), dict) else {}
    return str(aliases.get(speaker, speaker))


def _mode_for_horizon(horizon: str, explicit: str | None) -> str:
    if explicit in {"short_term", "medium_term", "long_term"}:
        return str(explicit)
    if horizon in {"days", "weeks"}:
        return "short_term"
    if horizon == "months":
        return "medium_term"
    return "long_term"


def _horizon_fit(claim_horizon: str | None, target: str) -> float:
    normalized = str(claim_horizon or "").lower()
    table = {
        "days": {"days": 1.0, "weeks": 0.62, "months": 0.28, "years": 0.12},
        "weeks": {"days": 0.7, "weeks": 1.0, "months": 0.62, "years": 0.25},
        "months": {"days": 0.35, "weeks": 0.68, "months": 1.0, "years": 0.55},
        "years": {"days": 0.18, "weeks": 0.3, "months": 0.65, "years": 1.0},
        "durable": {"days": 0.72, "weeks": 0.82, "months": 0.92, "years": 1.0},
    }
    return table.get(normalized, {"days": 0.6, "weeks": 0.7, "months": 0.7, "years": 0.6}).get(target, 0.6)


def _detected_domains(config: Mapping[str, Any], question: str) -> set[str]:
    keyword_map = config.get("domain_keywords") if isinstance(config.get("domain_keywords"), dict) else {}
    text = question.casefold()
    found = {
        str(domain) for domain, keywords in keyword_map.items()
        if isinstance(keywords, list) and any(str(keyword).casefold() in text for keyword in keywords)
    }
    return found or {"market_tactical", "portfolio_management", "risk_stress"}


def _claim_domains(config: Mapping[str, Any], claim: Mapping[str, Any]) -> set[str]:
    keyword_map = config.get("domain_keywords") if isinstance(config.get("domain_keywords"), dict) else {}
    text = " ".join([
        str(claim.get("statement") or ""),
        str(claim.get("claim_type") or ""),
        " ".join(str(value) for value in claim.get("subject_refs") or []),
    ]).casefold()
    found = {
        str(domain) for domain, keywords in keyword_map.items()
        if isinstance(keywords, list) and any(str(keyword).casefold() in text for keyword in keywords)
    }
    return found or {"market_tactical"}


def _claim_approaches(config: Mapping[str, Any], claim: Mapping[str, Any]) -> set[str]:
    rules = config.get("claim_approaches") if isinstance(config.get("claim_approaches"), dict) else {}
    text = " ".join([str(claim.get("statement") or ""), str(claim.get("claim_type") or "")]).casefold()
    matched: set[str] = set()
    for name, rule in rules.items():
        if not isinstance(rule, dict):
            continue
        claim_types = {str(value).casefold() for value in rule.get("claim_types") or []}
        keywords = [str(value).casefold() for value in rule.get("keywords") or []]
        if str(claim.get("claim_type") or "").casefold() in claim_types or any(value in text for value in keywords):
            matched.add(str(name))
    return matched


def build_opinion_consensus(
    config: Mapping[str, Any],
    claims: list[dict[str, Any]],
    *,
    question: str,
    horizon: str,
    mode: str | None = None,
) -> dict[str, Any]:
    validate_opinion_weighting_config(config)
    if horizon not in {"days", "weeks", "months", "years"}:
        raise OpinionWeightingError("opinion_consensus_horizon_invalid")
    mode_name = _mode_for_horizon(horizon, mode)
    mode_policy = config["mode_policies"][mode_name]
    prefs = config.get("user_preferences") or {}
    multipliers = {
        _canonical_speaker(config, str(key)): float(value)
        for key, value in (prefs.get("speaker_multipliers") or {}).items()
    }
    approach_multipliers = prefs.get("speaker_approach_multipliers") or {}
    excluded = {_canonical_speaker(config, str(value)) for value in prefs.get("excluded_speakers") or []}
    domains = _detected_domains(config, question)
    confidence_score = {"high": 1.0, "medium": 0.8, "low": 0.6, "unknown": 0.5}
    provenance_score = {"verified": 1.0, "partial": 0.8, "unverified": 0.55, "conflicted": 0.25}
    fidelity_score = {"direct": 1.0, "faithful_paraphrase": 0.85, "inference": 0.6, "unknown": 0.5}

    by_speaker: dict[str, list[tuple[float, dict[str, Any], float]]] = {}
    for raw in claims:
        if not isinstance(raw, dict) or raw.get("registration_state") != "canonical":
            continue
        if raw.get("truth_status") not in {"attributed_opinion", "estimate", "hypothesis"}:
            continue
        if raw.get("applicability_status") in {"stale", "expired", "out_of_scope"}:
            continue
        speaker_raw = str(raw.get("speaker") or "").strip()
        if not speaker_raw:
            continue
        speaker = _canonical_speaker(config, speaker_raw)
        if speaker in excluded:
            continue
        claim_domains = _claim_domains(config, raw)
        overlap = len(domains & claim_domains)
        relevance = 1.0 if overlap else 0.35
        base = (
            confidence_score.get(str(raw.get("confidence") or "unknown"), 0.5)
            * provenance_score.get(str(raw.get("provenance_verification") or "unverified"), 0.55)
            * fidelity_score.get(str(raw.get("semantic_fidelity") or "unknown"), 0.5)
            * _horizon_fit(raw.get("horizon"), horizon)
            * relevance
        )
        method_factor = 1.0
        speaker_method = approach_multipliers.get(speaker_raw) or approach_multipliers.get(speaker) or {}
        if isinstance(speaker_method, dict):
            for approach in _claim_approaches(config, raw):
                if approach in speaker_method:
                    method_factor *= float(speaker_method[approach])
        by_speaker.setdefault(speaker, []).append((base, dict(raw), method_factor))

    max_claims = int((config.get("policy") or {}).get("max_claims_per_speaker", 3))
    details: dict[str, dict[str, Any]] = {}
    for speaker, rows in by_speaker.items():
        ranked = sorted(rows, key=lambda item: item[0], reverse=True)[:max_claims]
        if not ranked:
            continue
        evidence_score = sum(item[0] for item in ranked) / len(ranked)
        method_factor = sum(item[2] for item in ranked) / len(ranked)
        details[speaker] = {
            "evidence_score": round(evidence_score, 6),
            "claim_method_usage_factor": round(method_factor, 6),
            "claim_ids": [str(item[1].get("claim_id") or "") for item in ranked],
        }

    anchors = {_canonical_speaker(config, str(key)): float(value) for key, value in mode_policy["anchor_weights"].items()}
    reserve = float(mode_policy["base_reserve"])
    effective: dict[str, float] = {}
    inactive: list[dict[str, Any]] = []
    minimum_reserve = float((config.get("policy") or {}).get("minimum_reserve", 0.1))
    for speaker, anchor in anchors.items():
        if speaker in excluded:
            reserve += anchor
            inactive.append({"speaker": speaker, "weight": anchor, "reason": "excluded"})
            continue
        if speaker not in details:
            reserve += anchor
            inactive.append({"speaker": speaker, "weight": anchor, "reason": "no_relevant_canonical_claim"})
            continue
        method_factor = float(details[speaker]["claim_method_usage_factor"])
        multiplier = float(multipliers.get(speaker, 1.0))
        desired = anchor * method_factor * multiplier
        if desired <= anchor:
            applied = desired
            reserve += anchor - applied
        else:
            extra = min(desired - anchor, max(0.0, reserve - minimum_reserve))
            applied = anchor + extra
            reserve -= extra
        effective[speaker] = applied

    supplemental_pool = float(mode_policy["supplemental_pool"])
    supplemental = [speaker for speaker in details if speaker not in anchors and speaker not in excluded]
    supplemental.sort(key=lambda speaker: details[speaker]["evidence_score"], reverse=True)
    supplemental = supplemental[:3]
    if supplemental:
        denominator = sum(max(1e-9, details[s]["evidence_score"]) for s in supplemental)
        for speaker in supplemental:
            effective[speaker] = supplemental_pool * max(1e-9, details[speaker]["evidence_score"]) / denominator
    else:
        reserve += supplemental_pool

    ordered = sorted(effective, key=effective.get, reverse=True)
    return {
        "policy_version": config["policy_version"],
        "mode": mode_name,
        "horizon": horizon,
        "question": question,
        "detected_domains": sorted(domains),
        "policy_anchor_weights": [{"speaker": speaker, "weight": anchors[speaker]} for speaker in anchors],
        "effective_weights": [
            {
                "speaker": speaker,
                "weight": round(effective[speaker], 6),
                "role": "anchor" if speaker in anchors else "supplemental",
                "evidence_score": details[speaker]["evidence_score"],
                "claim_ids": details[speaker]["claim_ids"],
            }
            for speaker in ordered
        ],
        "reserve_weight": round(reserve, 6),
        "inactive_anchors": inactive,
        "decision_usage": config.get("decision_usage"),
        "usage_note": "Opinion weighting organizes attributed source views only; it does not authorize an investment action or order.",
    }


def get_opinion_weighting_result(store: OpinionWeightingStore) -> dict[str, Any]:
    config = store.read()
    return result_envelope(
        capability="opinion.weighting.current",
        producer="investkitchen.opinion-weighting",
        status="ok",
        data={
            "policy_version": config["policy_version"],
            "policy_status": config.get("policy_status"),
            "decision_usage": config.get("decision_usage"),
            "mode_policies": copy.deepcopy(config["mode_policies"]),
            "user_preferences": copy.deepcopy(config.get("user_preferences") or {}),
        },
        authority="user_policy_record",
        freshness="local",
        source_mode="local_store",
        permissions_used=["opinion.read"],
    )


__all__ = [
    "OpinionWeightingError",
    "OpinionWeightingStore",
    "apply_weighting_update",
    "build_opinion_consensus",
    "build_weighting_update_preview",
    "get_opinion_weighting_result",
    "validate_opinion_weighting_config",
]
