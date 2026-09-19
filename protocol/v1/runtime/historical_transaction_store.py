from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Iterable

from protocol.v1.adapters.common import canonical_json, digest


STORE_SCHEMA_VERSION = 1
DEFAULT_MONTHS = ("2026-07", "2026-08", "2026-09")
USD_SYMBOLS = {"AAPL", "JEPQ", "DRAM", "IAU", "JEPI", "QQQM"}


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _tp(raw: Any) -> dict[str, str] | None:
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    return {"value": text, "precision": "source_exact" if "T" in text else "date_only"}


def _opaque_evidence_ref(relative_path: str, line_number: int, source_sha256: str) -> str:
    value = digest([relative_path, line_number, source_sha256])[:24]
    return f"evidence-ref:{value}"


def _execution_basis(source_type: Any) -> str:
    raw = str(source_type or "").lower()
    if "official" in raw or "execution_notice" in raw or "execution_sms" in raw:
        return "official_notice"
    if "explicit_user" in raw:
        return "explicit_user_confirmation"
    if "snapshot" in raw or "balance" in raw or "reconciled" in raw:
        return "balance_reconciliation"
    if "broker" in raw and "fill" in raw:
        return "provider_fill"
    return "other"


def _transaction_type_and_side(side: Any) -> tuple[str, str | None]:
    raw = str(side or "").lower()
    if raw in {"buy", "sell"}:
        return "trade", raw
    if raw in {"transfer_in", "transfer_out", "cash_transfer_in", "cash_transfer_out"}:
        return "cash_transfer", raw.removeprefix("cash_")
    if raw == "deposit":
        return "deposit", raw
    if raw == "withdrawal":
        return "withdrawal", raw
    return "other", raw or None


def _asset(row: dict[str, Any], currency: str) -> dict[str, Any] | None:
    tx_type, _ = _transaction_type_and_side(row.get("side"))
    if tx_type != "trade" and not row.get("quote_symbol"):
        return None
    symbol = row.get("quote_symbol")
    return {
        "asset_type": "unknown",
        "symbol": str(symbol) if symbol is not None else None,
        "venue": None,
        "currency": currency,
        "display_name": str(row.get("name") or symbol or "Unknown asset"),
        "provider_refs": {},
    }


def _money(amount: Any, currency: str, source_ref: str, *, derived: bool) -> dict[str, Any] | None:
    if amount is None:
        return None
    return {
        "amount": float(amount),
        "currency": currency,
        "value_basis": "derived" if derived else "observed",
        "source_evidence": [source_ref],
    }


def _canonical_transaction(
    row: dict[str, Any],
    *,
    portfolio_id: str,
    relative_path: str,
    line_number: int,
    source_sha256: str,
    base_currency: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    transaction_id = str(row.get("transaction_id") or "").strip()
    if not transaction_id:
        raise ValueError("legacy historical transaction_id is missing")
    account_id = row.get("account_id")
    source_ref = _opaque_evidence_ref(relative_path, line_number, source_sha256)
    if not isinstance(account_id, str) or not account_id.strip():
        return None, {
            "gap_code": "historical_transaction_account_unresolved",
            "transaction_id": transaction_id,
            "portfolio_id": portfolio_id,
            "source_evidence": source_ref,
            "source_locator": {"relative_path": relative_path, "line_number": line_number},
            "reason": "legacy transaction has no resolved account_id; migration does not infer an account",
        }

    tx_type, side = _transaction_type_and_side(row.get("side"))
    symbol = row.get("quote_symbol")
    currency = str(row.get("quote_currency") or ("USD" if symbol in USD_SYMBOLS else base_currency))
    basis = _execution_basis(row.get("source_type"))
    derived = basis == "balance_reconciliation"
    effective = _tp(row.get("trade_date")) or _tp(row.get("confirmed_on"))
    if effective is None:
        raise ValueError(f"legacy transaction effective time missing: {transaction_id}")

    status = str(row.get("status") or "").lower()
    occurrence_status = "confirmed" if status.startswith("confirmed") else "candidate"
    if tx_type == "trade":
        detail_status = (
            "complete"
            if row.get("price") is not None and row.get("fees_krw") is not None and row.get("taxes_krw") is not None
            else "partial"
        )
    else:
        detail_status = "complete" if row.get("gross_amount") is not None else "partial"

    transaction: dict[str, Any] = {
        "transaction_id": transaction_id,
        "portfolio_id": portfolio_id,
        "account_id": account_id,
        "transaction_type": tx_type,
        "side": side,
        "quantity": float(row["quantity"]) if row.get("quantity") is not None else None,
        "price": _money(row.get("price"), currency, source_ref, derived=derived),
        "amount": _money(row.get("gross_amount"), currency, source_ref, derived=derived),
        "effective_at": effective,
        "occurrence_status": occurrence_status,
        "detail_status": detail_status,
        "execution_basis": basis,
        "source_type": str(row.get("source_type") or "legacy_unknown"),
        "source_evidence": [source_ref],
        "provider_execution_id": row.get("provider_execution_id"),
        "transfer_group_id": row.get("transfer_group_id"),
        "lineage": None,
    }
    recorded = _tp(row.get("confirmed_on"))
    if recorded is not None:
        transaction["recorded_at"] = recorded
    asset = _asset(row, currency)
    if asset is not None:
        transaction["asset"] = asset
    return transaction, None


class HistoricalTransactionStore:
    """Immutable, non-materializing historical Transaction source store.

    This store is intentionally separate from NativeWriteStore. Its records may
    be read-unioned into transaction.history, but Portfolio checkpoint replay and
    materialization never consume this store.
    """

    def __init__(self, root: Path) -> None:
        self.root = root.expanduser().resolve()
        self.manifest_path = self.root / "manifest.json"
        self.transactions_path = self.root / "transactions.jsonl"
        self.gaps_path = self.root / "gaps.json"

    def exists(self) -> bool:
        return self.manifest_path.is_file() and self.transactions_path.is_file() and self.gaps_path.is_file()

    def validate(self) -> dict[str, Any]:
        manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("schema_version") != STORE_SCHEMA_VERSION:
            raise ValueError("historical transaction manifest invalid")
        transactions: list[dict[str, Any]] = []
        ids: set[str] = set()
        for line_number, raw in enumerate(self.transactions_path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            wrapper = json.loads(raw)
            if not isinstance(wrapper, dict) or set(wrapper) != {"store_version", "transaction", "migration_source"}:
                raise ValueError(f"historical transaction row invalid: {line_number}")
            if wrapper.get("store_version") != STORE_SCHEMA_VERSION or not isinstance(wrapper.get("transaction"), dict):
                raise ValueError(f"historical transaction row invalid: {line_number}")
            tx = wrapper["transaction"]
            txid = str(tx.get("transaction_id") or "")
            if not txid or txid in ids:
                raise ValueError("historical transaction ids are missing or duplicated")
            ids.add(txid)
            transactions.append(tx)
        gaps = json.loads(self.gaps_path.read_text(encoding="utf-8"))
        if not isinstance(gaps, list):
            raise ValueError("historical transaction gaps invalid")
        summary = manifest.get("summary") if isinstance(manifest.get("summary"), dict) else {}
        if summary.get("imported_transactions") != len(transactions) or summary.get("gaps") != len(gaps):
            raise ValueError("historical transaction store counts mismatch")
        if manifest.get("transactions_sha256") != _sha256_file(self.transactions_path):
            raise ValueError("historical transaction store digest mismatch")
        if manifest.get("gaps_sha256") != _sha256_file(self.gaps_path):
            raise ValueError("historical transaction gap digest mismatch")
        return manifest

    def list_transactions(self) -> list[dict[str, Any]]:
        self.validate()
        rows: list[dict[str, Any]] = []
        for raw in self.transactions_path.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                rows.append(json.loads(raw)["transaction"])
        return rows



__all__ = ["HistoricalTransactionStore"]
