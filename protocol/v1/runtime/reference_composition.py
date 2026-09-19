from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from protocol.v1.adapters.common import digest, result_envelope, timepoint
from protocol.v1.adapters.gateway_facade import ReadOnlyGatewayFacade, build_compatibility_handlers
from protocol.v1.adapters.native_history import get_decision_history, get_transaction_history
from protocol.v1.adapters.advisory_state import get_active_plans, get_current_policy, get_open_items
from protocol.v1.adapters.native_personal import (
    get_current_knowledge as get_native_current_knowledge,
    get_portfolio_state as get_native_portfolio_state,
    search_knowledge as search_native_knowledge,
)
from protocol.v1.adapters.materialized_portfolio import get_materialized_portfolio_state
from protocol.v1.runtime.native_write_store import NativeWriteStore
from protocol.v1.runtime.advisory_state_store import AdvisoryPlanStore, AdvisoryPolicyStore, OpenItemStore
from protocol.v1.runtime.advisory_policy_update import (
    apply_policy_update,
    build_policy_update_preview,
)
from protocol.v1.runtime.historical_decision_store import HistoricalDecisionStore
from protocol.v1.runtime.historical_transaction_store import HistoricalTransactionStore
from protocol.v1.runtime.native_knowledge_store import (
    NativeKnowledgeStore,
    apply_knowledge_commit,
    build_knowledge_preview,
)
from protocol.v1.runtime.portfolio_checkpoint import (
    PortfolioCheckpointStore,
    project_from_latest_checkpoint_through_cursor,
)
from protocol.v1.runtime.personal_data_store import PersonalDataStore
from protocol.v1.runtime.portfolio_reconciliation import journal_cursor_for_snapshot
from protocol.v1.runtime.portfolio_update_service import (
    accept_portfolio_update,
    approval_phrase,
    build_portfolio_update_preview,
    new_approval,
)
from protocol.v1.runtime.portfolio_account_sync import (
    AccountBindingRegistry,
    AccountSnapshotReader,
    accept_account_sync,
    build_account_sync_preview,
)
from protocol.v1.runtime.opinion_weighting import (
    OpinionWeightingStore,
    apply_weighting_update,
    build_opinion_consensus,
    build_weighting_update_preview,
    get_opinion_weighting_result,
)
from protocol.v1.security.trusted_approval import TrustedApprovalStore


MarketHandler = Callable[[dict[str, Any]], dict[str, Any]]


class ConnectorWriteRejected(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_timepoint(value: Any) -> datetime:
    raw = value.get("value") if isinstance(value, dict) else None
    if not isinstance(raw, str):
        raise ConnectorWriteRejected("write_identity_invalid")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ConnectorWriteRejected("write_identity_invalid") from exc
    if parsed.tzinfo is None:
        raise ConnectorWriteRejected("write_identity_invalid")
    return parsed.astimezone(timezone.utc)


class _ConnectorWriteCoordinator:
    """Process-local preview binding for the personal MCP connector.

    Canonical persistence remains in the existing domain stores. The connector
    only retains pending previews; process restart therefore invalidates pending
    approval and fails closed instead of replaying stale client state.
    """

    def __init__(
        self,
        *,
        native_store: NativeWriteStore,
        native_knowledge: NativeKnowledgeStore,
        checkpoint_store: PortfolioCheckpointStore | None,
        approval_store: TrustedApprovalStore,
        principal: dict[str, Any],
        grant: dict[str, Any],
        account_registry: AccountBindingRegistry | None = None,
        account_providers: Mapping[str, AccountSnapshotReader] | None = None,
        account_sync_max_age_seconds: int = 300,
        advisory_policies: AdvisoryPolicyStore | None = None,
        opinion_weighting: OpinionWeightingStore | None = None,
    ) -> None:
        self.native_store = native_store
        self.native_knowledge = native_knowledge
        self.checkpoint_store = checkpoint_store
        self.approval_store = approval_store
        self.principal = deepcopy(principal)
        self.grant = deepcopy(grant)
        self.account_registry = account_registry
        self.account_providers = dict(account_providers or {})
        self.account_sync_max_age_seconds = int(account_sync_max_age_seconds)
        self.advisory_policies = advisory_policies
        self.opinion_weighting = opinion_weighting
        self.pending: dict[str, dict[str, Any]] = {}
        self.consumed: set[str] = set()

    def _authorize(self, permission: str | Iterable[str], *, portfolio_id: str | None = None, approve: bool = False) -> None:
        now = _utc_now()
        for field in ("subject_user_id", "client_id", "credential_binding_id"):
            if str(self.principal.get(field) or "") != str(self.grant.get(field) or ""):
                raise ConnectorWriteRejected("write_identity_mismatch")
        if _parse_timepoint(self.principal.get("expires_at")) <= now or _parse_timepoint(self.grant.get("expires_at")) <= now:
            raise ConnectorWriteRejected("write_authority_expired")
        permissions = {str(value) for value in self.grant.get("permissions") or []}
        required = ({permission} if isinstance(permission, str) else {str(value) for value in permission}) | ({"operation.approve"} if approve else set())
        if not required.issubset(permissions):
            raise ConnectorWriteRejected("write_permission_denied")
        if portfolio_id is not None:
            scopes = {str(value) for value in self.grant.get("portfolio_scope") or []}
            if portfolio_id not in scopes:
                raise ConnectorWriteRejected("portfolio_scope_denied")

    def _register(self, preview_id: str, value: dict[str, Any]) -> None:
        if preview_id in self.pending or preview_id in self.consumed:
            raise ConnectorWriteRejected("preview_identity_conflict")
        self.pending[preview_id] = value

    def _consume_pending(self, preview_id: str, kind: str) -> dict[str, Any]:
        if preview_id in self.consumed:
            raise ConnectorWriteRejected("preview_replayed")
        pending = self.pending.pop(preview_id, None)
        if not isinstance(pending, dict) or pending.get("kind") != kind:
            raise ConnectorWriteRejected("preview_not_found_or_expired")
        self.consumed.add(preview_id)
        return pending

    def preview_knowledge(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._authorize("knowledge.commit")
        update = payload.get("update")
        if not isinstance(update, dict):
            raise ConnectorWriteRejected("knowledge_update_invalid")
        generation_id = str(update.get("generation_id") or "")
        if generation_id and any(
            str(row.get("generation_id") or "") == generation_id
            for row in self.native_knowledge.read_journal()
        ):
            raise ConnectorWriteRejected("knowledge_generation_already_committed")
        preview = build_knowledge_preview(
            update,
            base_generation_id=self.native_knowledge.latest_generation_id(),
        )
        mutation = preview["preview"]
        preview_id = str(mutation["preview_id"])
        confirmation = f"APPROVE KNOWLEDGE UPDATE {str(mutation['payload_digest'])[:12]}"
        self._register(preview_id, {
            "kind": "knowledge",
            "preview": preview,
            "confirmation": confirmation,
        })
        request = mutation["canonical_payload"]
        return {
            "status": "review_required",
            "preview_id": preview_id,
            "payload_digest": mutation["payload_digest"],
            "base_generation_id": mutation.get("base_version"),
            "generation_id": request["generation_id"],
            "evidence_count": len(request.get("evidence") or []),
            "claim_count": len(request.get("claims") or []),
            "current_state_fields": sorted((request.get("current_state") or {}).keys()),
            "confirmation": confirmation,
        }

    def apply_knowledge(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._authorize("knowledge.commit", approve=True)
        preview_id = str(payload.get("preview_id") or "")
        pending = self._consume_pending(preview_id, "knowledge")
        confirmation = payload.get("confirmation")
        if confirmation != pending["confirmation"]:
            raise ConnectorWriteRejected("confirmation_mismatch")
        preview = pending["preview"]
        if preview["preview"].get("base_version") != self.native_knowledge.latest_generation_id():
            raise ConnectorWriteRejected("preview_stale")
        approval = self.approval_store.issue(
            preview,
            principal=self.principal,
            grant=self.grant,
            interaction_ref="mcp-confirmation:" + digest([preview_id, confirmation])[:24],
            approval_method="mcp_explicit_confirmation",
            ttl_seconds=300,
        )
        result = apply_knowledge_commit(
            preview,
            approval,
            principal=self.principal,
            grant=self.grant,
            approval_verifier=self.approval_store.verify,
            store=self.native_knowledge,
        )
        if result.get("replayed") is True:
            raise ConnectorWriteRejected("write_replay_detected")
        generation_id = str(result["generation_id"])
        return {
            "status": "applied",
            "preview_id": preview_id,
            "generation_id": generation_id,
            "payload_digest": result["payload_digest"],
            "committed_at": result["committed_at"],
            "read_back": {
                "generation_id": self.native_knowledge.latest_generation_id(),
                "matches": self.native_knowledge.latest_generation_id() == generation_id,
            },
        }

    def preview_portfolio(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.checkpoint_store is None:
            raise ConnectorWriteRejected("portfolio_update_unavailable")
        update = payload.get("update")
        if not isinstance(update, dict):
            raise ConnectorWriteRejected("portfolio_update_invalid")
        portfolio_id = str(update.get("portfolio_id") or "")
        self._authorize("portfolio.update", portfolio_id=portfolio_id)
        result = build_portfolio_update_preview(
            update,
            checkpoint_store=self.checkpoint_store,
            write_store=self.native_store,
        )
        candidate = result["candidate"]
        review = result["review"]
        preview_id = str(candidate["candidate_id"])
        self._register(preview_id, {
            "kind": "portfolio",
            "candidate": candidate,
            "confirmation": review["approval_phrase"],
            "portfolio_id": portfolio_id,
            "base_checkpoint_id": self.checkpoint_store.latest(portfolio_id).get("checkpoint_id"),
        })
        return {
            "status": "review_required",
            "preview_id": preview_id,
            "candidate_digest": candidate["candidate_digest"],
            "portfolio_id": portfolio_id,
            "snapshot_effective_at": review["snapshot_effective_at"],
            "summary": review["summary"],
            "differences": review["differences"],
            "gaps": review["gaps"],
            "confirmation": review["approval_phrase"],
        }

    def apply_portfolio(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.checkpoint_store is None:
            raise ConnectorWriteRejected("portfolio_update_unavailable")
        preview_id = str(payload.get("preview_id") or "")
        pending = self._consume_pending(preview_id, "portfolio")
        portfolio_id = str(pending["portfolio_id"])
        self._authorize("portfolio.update", portfolio_id=portfolio_id, approve=True)
        confirmation = payload.get("confirmation")
        if confirmation != pending["confirmation"]:
            raise ConnectorWriteRejected("confirmation_mismatch")
        candidate = pending["candidate"]
        latest_checkpoint = self.checkpoint_store.latest(portfolio_id)
        if latest_checkpoint.get("checkpoint_id") != pending.get("base_checkpoint_id"):
            raise ConnectorWriteRejected("preview_stale")
        current_cursor = journal_cursor_for_snapshot(
            self.native_store,
            portfolio_id=portfolio_id,
            snapshot_effective_at=candidate["snapshot_effective_at"],
        )
        if current_cursor != candidate.get("journal_cursor"):
            raise ConnectorWriteRejected("preview_stale")
        projection = project_from_latest_checkpoint_through_cursor(
            self.checkpoint_store,
            self.native_store,
            portfolio_id=portfolio_id,
            through_commit_id=current_cursor.get("through_commit_id"),
            generated_at=candidate.get("created_at"),
        )
        current_portfolio = projection.get("materialization", {}).get("portfolio")
        if not isinstance(current_portfolio, dict) or digest(current_portfolio) != candidate.get("current_portfolio_digest"):
            raise ConnectorWriteRejected("preview_stale")
        approved_point = max(_utc_now(), _parse_timepoint(candidate.get("created_at")))
        approved_at = timepoint(approved_point.isoformat().replace("+00:00", "Z"))
        approval = new_approval(
            candidate,
            approval_ref="mcp-confirmation:" + digest([preview_id, confirmation, uuid.uuid4().hex])[:24],
            approved_at=approved_at,
        )

        expected_candidate_digest = digest(candidate)
        expected_approval_digest = digest(approval)

        def verify(candidate_value: dict[str, Any], approval_value: dict[str, Any]) -> dict[str, Any]:
            verified = (
                digest(candidate_value) == expected_candidate_digest
                and digest(approval_value) == expected_approval_digest
                and approval_value.get("confirmation") == confirmation
            )
            return {
                "verified": verified,
                "verification_ref": (
                    "mcp-portfolio-approval:" + digest([preview_id, expected_approval_digest])[:24]
                    if verified else None
                ),
            }

        result = accept_portfolio_update(
            candidate,
            approval,
            checkpoint_store=self.checkpoint_store,
            write_store=self.native_store,
            approval_verifier=verify,
        )
        latest = self.checkpoint_store.latest(portfolio_id)
        read_back_projection = project_from_latest_checkpoint_through_cursor(
            self.checkpoint_store,
            self.native_store,
            portfolio_id=portfolio_id,
            through_commit_id=candidate.get("journal_cursor", {}).get("through_commit_id"),
            generated_at=candidate.get("snapshot_effective_at"),
        )
        read_back_portfolio = read_back_projection.get("materialization", {}).get("portfolio")
        state_matches = (
            isinstance(read_back_portfolio, dict)
            and digest(read_back_portfolio) == digest(candidate.get("reconciled_portfolio"))
        )
        if not state_matches:
            raise ConnectorWriteRejected("portfolio_readback_mismatch")
        return {
            "status": "applied",
            "preview_id": preview_id,
            "portfolio_id": portfolio_id,
            "candidate_digest": result["candidate_digest"],
            "checkpoint_id": result["checkpoint_id"],
            "accepted_at": result["accepted_at"],
            "read_back": {
                "checkpoint_id": latest.get("checkpoint_id"),
                "candidate_ref": latest.get("reconciliation_candidate_ref"),
                "matches": (
                    latest.get("checkpoint_id") == result["checkpoint_id"]
                    and latest.get("reconciliation_candidate_ref") == candidate["candidate_id"]
                    and state_matches
                ),
                "state_digest": digest(read_back_portfolio) if isinstance(read_back_portfolio, dict) else None,
            },
        }

    def preview_account_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.checkpoint_store is None or self.account_registry is None or not self.account_providers:
            raise ConnectorWriteRejected("account_sync_unavailable")
        portfolio_id = str(payload.get("portfolio_id") or "")
        account_id = str(payload.get("account_id") or "")
        if not portfolio_id or not account_id:
            raise ConnectorWriteRejected("account_sync_scope_invalid")
        self._authorize({"account.read", "portfolio.update"}, portfolio_id=portfolio_id)
        result = build_account_sync_preview(
            portfolio_id=portfolio_id,
            account_id=account_id,
            registry=self.account_registry,
            providers=self.account_providers,
            checkpoint_store=self.checkpoint_store,
            write_store=self.native_store,
            max_age_seconds=self.account_sync_max_age_seconds,
        )
        if result.get("status") == "no_change":
            return {
                "status": "no_change",
                "portfolio_id": portfolio_id,
                "account_id": account_id,
                "provider_id": result.get("provider_id"),
                "snapshot_effective_at": result.get("snapshot_effective_at"),
                "summary": result.get("summary"),
            }
        candidate = result.get("candidate")
        review = result.get("review")
        if not isinstance(candidate, dict) or not isinstance(review, dict):
            raise ConnectorWriteRejected("account_sync_preview_invalid")
        preview_id = str(candidate.get("candidate_id") or "")
        self._register(preview_id, {
            "kind": "account_sync",
            "sync_preview": result,
            "candidate": candidate,
            "confirmation": review["approval_phrase"],
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "base_checkpoint_id": self.checkpoint_store.latest(portfolio_id).get("checkpoint_id"),
        })
        return {
            "status": "review_required",
            "preview_id": preview_id,
            "candidate_digest": candidate["candidate_digest"],
            "portfolio_id": portfolio_id,
            "account_id": account_id,
            "provider_id": result.get("provider_id"),
            "snapshot_effective_at": result.get("snapshot_effective_at"),
            "summary": review.get("summary"),
            "differences": review.get("differences"),
            "gaps": review.get("gaps"),
            "confirmation": review["approval_phrase"],
        }

    def apply_account_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.checkpoint_store is None:
            raise ConnectorWriteRejected("account_sync_unavailable")
        preview_id = str(payload.get("preview_id") or "")
        pending = self._consume_pending(preview_id, "account_sync")
        portfolio_id = str(pending.get("portfolio_id") or "")
        self._authorize("portfolio.update", portfolio_id=portfolio_id, approve=True)
        confirmation = payload.get("confirmation")
        if confirmation != pending.get("confirmation"):
            raise ConnectorWriteRejected("confirmation_mismatch")
        candidate = pending.get("candidate")
        sync_preview = pending.get("sync_preview")
        if not isinstance(candidate, dict) or not isinstance(sync_preview, dict):
            raise ConnectorWriteRejected("account_sync_preview_invalid")
        latest_checkpoint = self.checkpoint_store.latest(portfolio_id)
        if latest_checkpoint.get("checkpoint_id") != pending.get("base_checkpoint_id"):
            raise ConnectorWriteRejected("preview_stale")
        current_cursor = journal_cursor_for_snapshot(
            self.native_store,
            portfolio_id=portfolio_id,
            snapshot_effective_at=candidate["snapshot_effective_at"],
        )
        if current_cursor != candidate.get("journal_cursor"):
            raise ConnectorWriteRejected("preview_stale")
        projection = project_from_latest_checkpoint_through_cursor(
            self.checkpoint_store,
            self.native_store,
            portfolio_id=portfolio_id,
            through_commit_id=current_cursor.get("through_commit_id"),
            generated_at=candidate.get("created_at"),
            identity_scope_account_ids={str(pending.get("account_id") or "")},
        )
        current_portfolio = projection.get("materialization", {}).get("portfolio")
        if not isinstance(current_portfolio, dict) or digest(current_portfolio) != candidate.get("current_portfolio_digest"):
            raise ConnectorWriteRejected("preview_stale")

        approved_point = max(_utc_now(), _parse_timepoint(candidate.get("created_at")))
        approved_at = timepoint(approved_point.isoformat().replace("+00:00", "Z"))
        approval = new_approval(
            candidate,
            approval_ref="mcp-account-sync:" + digest([preview_id, confirmation, uuid.uuid4().hex])[:24],
            approved_at=approved_at,
        )
        expected_candidate_digest = digest(candidate)
        expected_approval_digest = digest(approval)

        def verify(candidate_value: dict[str, Any], approval_value: dict[str, Any]) -> dict[str, Any]:
            verified = (
                digest(candidate_value) == expected_candidate_digest
                and digest(approval_value) == expected_approval_digest
                and approval_value.get("confirmation") == confirmation
            )
            return {
                "verified": verified,
                "verification_ref": (
                    "mcp-account-sync-approval:" + digest([preview_id, expected_approval_digest])[:24]
                    if verified else None
                ),
            }

        result = accept_account_sync(
            sync_preview,
            approval,
            checkpoint_store=self.checkpoint_store,
            write_store=self.native_store,
            approval_verifier=verify,
        )
        return {
            **result,
            "preview_id": preview_id,
        }

    def preview_policy_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.advisory_policies is None:
            raise ConnectorWriteRejected("policy_update_unavailable")
        update = payload.get("update")
        if not isinstance(update, dict):
            raise ConnectorWriteRejected("policy_update_invalid")
        portfolio_id = str(update.get("portfolio_id") or "")
        self._authorize("policy.update", portfolio_id=portfolio_id)
        preview = build_policy_update_preview(self.advisory_policies, update)
        preview_id = str(preview["preview_id"])
        self._register(preview_id, {
            "kind": "policy_update",
            "preview": preview,
            "confirmation": preview["confirmation"],
            "portfolio_id": portfolio_id,
        })
        return {
            "status": "review_required",
            "preview_id": preview_id,
            "candidate_digest": preview["candidate_digest"],
            "portfolio_id": portfolio_id,
            "current_policy_id": preview["current_policy_id"],
            "next_policy_id": preview["next_policy"]["policy_id"],
            "changed_rule_keys": preview["changed_rule_keys"],
            "confirmation": preview["confirmation"],
        }

    def apply_policy_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.advisory_policies is None:
            raise ConnectorWriteRejected("policy_update_unavailable")
        preview_id = str(payload.get("preview_id") or "")
        pending = self._consume_pending(preview_id, "policy_update")
        portfolio_id = str(pending.get("portfolio_id") or "")
        self._authorize("policy.update", portfolio_id=portfolio_id, approve=True)
        confirmation = payload.get("confirmation")
        if confirmation != pending.get("confirmation"):
            raise ConnectorWriteRejected("confirmation_mismatch")
        result = apply_policy_update(self.advisory_policies, pending["preview"])
        return {**result, "preview_id": preview_id}

    def preview_opinion_weighting_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.opinion_weighting is None:
            raise ConnectorWriteRejected("opinion_weighting_update_unavailable")
        update = payload.get("update")
        if not isinstance(update, dict):
            raise ConnectorWriteRejected("opinion_weighting_update_invalid")
        self._authorize("opinion.update")
        preview = build_weighting_update_preview(self.opinion_weighting, update)
        preview_id = str(preview["preview_id"])
        self._register(preview_id, {
            "kind": "opinion_weighting_update",
            "preview": preview,
            "confirmation": preview["confirmation"],
        })
        return {
            "status": "review_required",
            "preview_id": preview_id,
            "candidate_digest": preview["candidate_digest"],
            "current_policy_version": preview["current_policy_version"],
            "next_policy_version": preview["next_policy_version"],
            "changed_sections": preview["changed_sections"],
            "confirmation": preview["confirmation"],
        }

    def apply_opinion_weighting_update(self, payload: dict[str, Any]) -> dict[str, Any]:
        if self.opinion_weighting is None:
            raise ConnectorWriteRejected("opinion_weighting_update_unavailable")
        preview_id = str(payload.get("preview_id") or "")
        pending = self._consume_pending(preview_id, "opinion_weighting_update")
        self._authorize("opinion.update", approve=True)
        confirmation = payload.get("confirmation")
        if confirmation != pending.get("confirmation"):
            raise ConnectorWriteRejected("confirmation_mismatch")
        result = apply_weighting_update(self.opinion_weighting, pending["preview"])
        return {**result, "preview_id": preview_id}

    def handlers(self) -> dict[str, Callable[[dict[str, Any]], dict[str, Any]]]:
        permissions = {str(value) for value in self.grant.get("permissions") or []}
        allowlist = {str(value) for value in self.grant.get("tool_allowlist") or []}

        def allowed(name: str) -> bool:
            return not allowlist or name in allowlist

        result: dict[str, Callable[[dict[str, Any]], dict[str, Any]]] = {}
        if "knowledge.commit" in permissions and allowed("preview_knowledge_update"):
            result["preview_knowledge_update"] = self.preview_knowledge
            if "operation.approve" in permissions and allowed("apply_knowledge_update"):
                result["apply_knowledge_update"] = self.apply_knowledge
        if self.checkpoint_store is not None and "portfolio.update" in permissions:
            if allowed("preview_portfolio_update"):
                result["preview_portfolio_update"] = self.preview_portfolio
            if "operation.approve" in permissions and allowed("apply_portfolio_update"):
                result["apply_portfolio_update"] = self.apply_portfolio
            if (
                self.account_registry is not None
                and self.account_providers
                and "account.read" in permissions
                and allowed("preview_account_sync")
            ):
                result["preview_account_sync"] = self.preview_account_sync
                if "operation.approve" in permissions and allowed("apply_account_sync"):
                    result["apply_account_sync"] = self.apply_account_sync
        if self.advisory_policies is not None and "policy.update" in permissions:
            if allowed("preview_policy_update"):
                result["preview_policy_update"] = self.preview_policy_update
            if "operation.approve" in permissions and allowed("apply_policy_update"):
                result["apply_policy_update"] = self.apply_policy_update
        if self.opinion_weighting is not None and "opinion.update" in permissions:
            if allowed("preview_opinion_weighting_update"):
                result["preview_opinion_weighting_update"] = self.preview_opinion_weighting_update
            if "operation.approve" in permissions and allowed("apply_opinion_weighting_update"):
                result["apply_opinion_weighting_update"] = self.apply_opinion_weighting_update
        return result


def _provider_child_env() -> dict[str, str]:
    """Return a deliberately small environment for provider subprocesses."""
    allowed = ("LANG", "LC_ALL", "TZ", "SSL_CERT_FILE")
    return {key: os.environ[key] for key in allowed if key in os.environ}


def _provider_unavailable(capability: str) -> dict[str, Any]:
    return result_envelope(
        capability=capability,
        producer="trademind.market.provider-process",
        status="unavailable",
        data=None,
        authority="market_observation",
        freshness="unknown",
        source_mode="live_fetch",
        warnings=["Market provider process is unavailable."],
        gaps=[{
            "gap_code": "market_provider_process_unavailable",
            "required_capability": capability,
            "scope": None,
            "reason": "The configured market provider process did not return a usable result.",
            "impact": "Current market observations are unavailable for this request.",
            "recoverable": True,
        }],
        permissions_used=["market.read"],
    )


def build_toss_readonly_subprocess_handlers(
    runtime_root: Path,
    *,
    secret_file: Path,
    provider_workspace: Path | None = None,
    timeout_seconds: float = 50.0,
) -> dict[str, MarketHandler]:
    """Bind self-contained Toss reads through a scrubbed provider subprocess."""
    runtime_root = runtime_root.resolve()
    provider_workspace = provider_workspace.resolve() if provider_workspace is not None else None
    secret_file = secret_file.resolve()
    if not secret_file.is_file():
        raise ValueError("market provider secret file is not configured")
    worker = runtime_root / "protocol" / "v1" / "providers" / "toss_readonly_worker.py"
    if not worker.is_file():
        raise ValueError("market provider worker is unavailable")
    cache: dict[str, tuple[float, dict[str, Any]]] = {}

    def cache_key(capability: str, payload: dict[str, Any]) -> str:
        normalized = deepcopy(payload)
        if isinstance(normalized.get("symbols"), list):
            normalized["symbols"] = sorted(str(value) for value in normalized["symbols"])
        return digest([capability, normalized])

    def invoke(capability: str, payload: dict[str, Any]) -> dict[str, Any]:
        key = cache_key(capability, payload)
        now = time.monotonic()
        cached = cache.get(key)
        if cached is not None and cached[0] > now:
            value = deepcopy(cached[1])
            value["source_mode"] = "cache"
            value["freshness"] = "cached"
            return value
        request = {"capability": capability, "input": dict(payload)}
        try:
            command = [sys.executable, str(worker)]
            if provider_workspace is not None:
                command.extend(["--provider-workspace", str(provider_workspace)])
            command.extend(["--secret-file", str(secret_file)])
            completed = subprocess.run(
                command,
                input=json.dumps(request, ensure_ascii=False),
                text=True,
                capture_output=True,
                env=_provider_child_env(),
                timeout=timeout_seconds,
                check=False,
            )
            if completed.returncode != 0 or not completed.stdout.strip():
                return _provider_unavailable(capability)
            value = json.loads(completed.stdout)
            result = value if isinstance(value, dict) else _provider_unavailable(capability)
            if result.get("status") in {"ok", "partial"}:
                ttl = 3.0 if capability == "market.quote" else 15.0
                cache[key] = (time.monotonic() + ttl, deepcopy(result))
            return result
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError):
            return _provider_unavailable(capability)

    return {
        "market.quote": lambda payload: invoke("market.quote", payload),
        "market.ohlcv": lambda payload: invoke("market.ohlcv", payload),
    }


def build_account_readonly_subprocess_provider(
    runtime_root: Path,
    *,
    provider_id: str,
    secret_file: Path,
    timeout_seconds: float = 50.0,
) -> AccountSnapshotReader:
    """Bind one credential-backed account reader behind a scrubbed subprocess.

    The parent process holds only the opaque provider account reference. Actual
    account numbers and credentials are resolved inside the child from the
    explicitly mounted secret file and are never returned to the MCP surface.
    """

    if provider_id not in {"toss_securities", "nhplug"}:
        raise ValueError("unsupported account provider binding")
    runtime_root = runtime_root.resolve()
    secret_file = secret_file.expanduser().resolve()
    if not secret_file.is_file():
        raise ValueError("account provider secret file is not configured")
    worker = runtime_root / "protocol" / "v1" / "providers" / "account_readonly_worker.py"
    if not worker.is_file():
        raise ValueError("account provider worker is unavailable")

    def invoke(binding: dict[str, Any]) -> dict[str, Any]:
        request = {"binding": deepcopy(binding)}
        try:
            completed = subprocess.run(
                [
                    sys.executable,
                    str(worker),
                    "--provider-id",
                    provider_id,
                    "--secret-file",
                    str(secret_file),
                ],
                input=json.dumps(request, ensure_ascii=False),
                text=True,
                capture_output=True,
                env=_provider_child_env(),
                timeout=timeout_seconds,
                check=False,
            )
            if completed.returncode != 0 or not completed.stdout.strip():
                raise RuntimeError("account_provider_process_unavailable")
            value = json.loads(completed.stdout)
            if not isinstance(value, dict):
                raise RuntimeError("account_provider_process_unavailable")
            return value
        except (OSError, subprocess.SubprocessError, json.JSONDecodeError) as exc:
            raise RuntimeError("account_provider_process_unavailable") from exc

    return invoke


def build_reference_gateway(
    *,
    runtime_root: Path,
    manifest: Path,
    legacy_workspace: Path | None = None,
    personal_data_root: Path | None = None,
    market_provider: str = "none",
    market_secret_file: Path | None = None,
    native_store_root: Path | None = None,
    advisory_state_root: Path | None = None,
    historical_decision_root: Path | None = None,
    historical_transaction_root: Path | None = None,
    portfolio_checkpoint_root: Path | None = None,
    approval_store_root: Path | None = None,
    write_principal: dict[str, Any] | None = None,
    write_grant: dict[str, Any] | None = None,
    account_bindings: Iterable[Mapping[str, Any]] | None = None,
    account_snapshot_providers: Mapping[str, AccountSnapshotReader] | None = None,
    account_provider_secret_files: Mapping[str, Path] | None = None,
    account_sync_max_age_seconds: int = 300,
) -> ReadOnlyGatewayFacade:
    runtime_root = runtime_root.resolve()
    legacy_workspace = legacy_workspace.resolve() if legacy_workspace is not None else None
    personal_data_root = personal_data_root.expanduser().resolve() if personal_data_root is not None else None
    market_handlers: dict[str, MarketHandler] | None = None
    if market_provider == "none":
        market_handlers = None
    elif market_provider in {"toss-readonly-subprocess", "toss-native-subprocess"}:
        if market_secret_file is None:
            raise ValueError("market provider secret file is required")
        market_handlers = build_toss_readonly_subprocess_handlers(
            runtime_root,
            secret_file=market_secret_file,
            provider_workspace=legacy_workspace if market_provider == "toss-readonly-subprocess" else None,
        )
    else:
        raise ValueError("unsupported market provider binding")

    configured_account_providers: dict[str, AccountSnapshotReader] = dict(account_snapshot_providers or {})
    for provider_id, secret_file in (account_provider_secret_files or {}).items():
        if provider_id in configured_account_providers:
            raise ValueError("account provider configured more than once")
        configured_account_providers[provider_id] = build_account_readonly_subprocess_provider(
            runtime_root,
            provider_id=provider_id,
            secret_file=secret_file,
        )
    if configured_account_providers and account_bindings is None:
        raise ValueError("account bindings are required for account providers")

    history_handlers = None
    connector_handlers = None
    native_store: NativeWriteStore | None = None
    native_knowledge: NativeKnowledgeStore | None = None
    historical_decisions: HistoricalDecisionStore | None = None
    historical_transactions: HistoricalTransactionStore | None = None
    open_items: OpenItemStore | None = None
    advisory_policies: AdvisoryPolicyStore | None = None
    advisory_plans: AdvisoryPlanStore | None = None
    opinion_weighting: OpinionWeightingStore | None = None
    personal_store: PersonalDataStore | None = None
    configured_portfolio_ids: list[str] = []
    if advisory_state_root is not None:
        root = advisory_state_root.resolve()
        candidate_open_items = OpenItemStore(root / "open-items")
        candidate_policies = AdvisoryPolicyStore(root / "policies")
        candidate_plans = AdvisoryPlanStore(root / "plans")
        candidate_opinion_weighting = OpinionWeightingStore(root / "opinion-weighting")
        if candidate_open_items.exists():
            candidate_open_items.validate()
            open_items = candidate_open_items
        if candidate_policies.exists():
            candidate_policies.validate()
            advisory_policies = candidate_policies
        if candidate_plans.exists():
            candidate_plans.validate()
            advisory_plans = candidate_plans
        if candidate_opinion_weighting.exists():
            candidate_opinion_weighting.validate()
            opinion_weighting = candidate_opinion_weighting
    if historical_decision_root is not None:
        candidate = HistoricalDecisionStore(historical_decision_root.resolve())
        if candidate.exists():
            candidate.validate()
            historical_decisions = candidate
    if historical_transaction_root is not None:
        candidate = HistoricalTransactionStore(historical_transaction_root.resolve())
        if candidate.exists():
            candidate.validate()
            historical_transactions = candidate
    if native_store_root is not None:
        native_store = NativeWriteStore(native_store_root.resolve())
        native_knowledge = NativeKnowledgeStore(native_store_root.resolve())
        history_handlers = {
            "decision.history": lambda payload: get_decision_history(
                native_store,
                portfolio_id=str(payload["portfolio_id"]),
                status=payload.get("status"),
                limit=int(payload.get("limit", 50)),
                historical_store=historical_decisions,
            ),
            "transaction.history": lambda payload: get_transaction_history(
                native_store,
                portfolio_id=str(payload["portfolio_id"]),
                account_id=str(payload["account_id"]) if payload.get("account_id") is not None else None,
                limit=int(payload.get("limit", 100)),
                historical_store=historical_transactions,
            ),
        }

    handlers = build_compatibility_handlers(
        legacy_workspace if personal_data_root is None else None,
        market_handlers=market_handlers,
        history_handlers=history_handlers,
    )
    if open_items is not None:
        handlers["openitem.current"] = lambda payload: get_open_items(
            open_items,
            portfolio_id=str(payload["portfolio_id"]),
            limit=int(payload.get("limit", 50)),
        )
    if advisory_policies is not None:
        handlers["policy.current"] = lambda payload: get_current_policy(
            advisory_policies,
            portfolio_id=str(payload["portfolio_id"]),
        )
    if advisory_plans is not None:
        handlers["plan.active"] = lambda payload: get_active_plans(
            advisory_plans,
            portfolio_id=str(payload["portfolio_id"]),
        )
    if opinion_weighting is not None:
        handlers["opinion.weighting.current"] = lambda _payload: get_opinion_weighting_result(opinion_weighting)
    if personal_data_root is not None:
        personal_store = PersonalDataStore(personal_data_root)
        verification = personal_store.verify()
        configured_portfolio_ids = [str(value) for value in verification.get("portfolio_ids") or []]
        handlers.update({
            "portfolio.state": lambda payload: get_native_portfolio_state(
                personal_store,
                str(payload["portfolio_id"]),
            ),
            "knowledge.current": lambda payload: get_native_current_knowledge(
                personal_store,
                max_outlook=int(payload.get("max_outlook", 20)),
                native_knowledge=native_knowledge,
            ),
            "knowledge.search": lambda payload: search_native_knowledge(
                personal_store,
                str(payload.get("query") or ""),
                limit=int(payload.get("limit", 20)),
                native_knowledge=native_knowledge,
            ),
        })
    elif native_knowledge is not None and native_knowledge.has_generations():
        handlers.update({
            "knowledge.current": lambda payload: get_native_current_knowledge(
                None,
                max_outlook=int(payload.get("max_outlook", 20)),
                native_knowledge=native_knowledge,
            ),
            "knowledge.search": lambda payload: search_native_knowledge(
                None,
                str(payload.get("query") or ""),
                limit=int(payload.get("limit", 20)),
                native_knowledge=native_knowledge,
            ),
        })
    if opinion_weighting is not None and (personal_store is not None or native_knowledge is not None):
        def opinion_claims() -> list[dict[str, Any]]:
            baseline = personal_store.knowledge_projection() if personal_store is not None else {"claims": []}
            by_claim = {
                str(row.get("claim_id")): dict(row)
                for row in baseline.get("claims") or []
                if isinstance(row, dict) and row.get("claim_id")
            }
            overlay = native_knowledge.overlay() if native_knowledge is not None else None
            if overlay is not None:
                for row in overlay.get("claims") or []:
                    if isinstance(row, dict) and row.get("claim_id"):
                        by_claim[str(row["claim_id"])] = dict(row)
            return list(by_claim.values())

        handlers["opinion.consensus"] = lambda payload: result_envelope(
            capability="opinion.consensus",
            producer="investkitchen.opinion-weighting",
            status="ok",
            data=build_opinion_consensus(
                opinion_weighting.read(),
                opinion_claims(),
                question=str(payload.get("question") or ""),
                horizon=str(payload.get("horizon") or "weeks"),
                mode=str(payload.get("mode")) if payload.get("mode") is not None else None,
            ),
            authority="derived_calculation",
            freshness="local",
            source_mode="local_store",
            permissions_used=["opinion.read", "knowledge.read"],
        )
    if portfolio_checkpoint_root is not None:
        if native_store is None:
            raise ValueError("portfolio checkpoint root requires native store root")
        checkpoint_store = PortfolioCheckpointStore(portfolio_checkpoint_root.resolve())
        handlers["portfolio.state"] = lambda payload: get_materialized_portfolio_state(
            checkpoint_store,
            native_store,
            str(payload["portfolio_id"]),
        )
    write_configured = any(value is not None for value in (
        approval_store_root,
        write_principal,
        write_grant,
    ))
    if write_configured:
        if native_store is None or native_knowledge is None:
            raise ValueError("connector writes require native store root")
        if approval_store_root is None or not isinstance(write_principal, dict) or not isinstance(write_grant, dict):
            raise ValueError("connector writes require approval store, principal, and grant")
        checkpoint_store = (
            PortfolioCheckpointStore(portfolio_checkpoint_root.resolve())
            if portfolio_checkpoint_root is not None else None
        )
        connector_handlers = _ConnectorWriteCoordinator(
            native_store=native_store,
            native_knowledge=native_knowledge,
            checkpoint_store=checkpoint_store,
            approval_store=TrustedApprovalStore(approval_store_root.resolve()),
            principal=write_principal,
            grant=write_grant,
            account_registry=AccountBindingRegistry(account_bindings or []) if account_bindings is not None else None,
            account_providers=configured_account_providers,
            account_sync_max_age_seconds=account_sync_max_age_seconds,
            advisory_policies=advisory_policies,
            opinion_weighting=opinion_weighting,
        ).handlers()
    return ReadOnlyGatewayFacade(
        workspace=legacy_workspace or runtime_root,
        manifest=manifest.resolve(),
        handlers=handlers,
        connector_handlers=connector_handlers,
        portfolio_ids=configured_portfolio_ids,
    )
