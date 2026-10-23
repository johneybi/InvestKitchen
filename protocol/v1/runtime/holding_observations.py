"""Provider-observed holding values, separate from quotes and trade accounting."""

from __future__ import annotations

import copy
import math
from typing import Any, Mapping


HOLDING_VALUE_FIELDS = ("cost_basis_values", "avg_cost_values", "valuation")
VALUATION_NUMBERS = (
    "last_price", "market_value", "market_value_after_cost",
    "profit_loss", "profit_loss_after_cost", "profit_loss_rate", "profit_loss_rate_after_cost",
)


def validate_holding_values(
    holding: Mapping[str, Any],
    *,
    currency: str,
    observed_at: dict[str, Any],
    source_ref: str,
) -> dict[str, Any]:
    """Validate normalized observations; never infer missing prices or costs."""

    def number(value: Any, *, nonnegative: bool = False) -> int | float:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("holding_value_number_invalid")
        try:
            finite = math.isfinite(value)
        except OverflowError:
            finite = False
        if not finite or (nonnegative and value < 0):
            raise ValueError("holding_value_number_invalid")
        return value

    def metadata(row: Mapping[str, Any], time_field: str) -> dict[str, Any]:
        if row.get("currency") != currency or row.get("value_basis") != "observed":
            raise ValueError("holding_value_basis_or_currency_invalid")
        if row.get(time_field, observed_at) != observed_at:
            raise ValueError("holding_value_time_invalid")
        evidence = row.get("source_evidence", [])
        if not isinstance(evidence, list) or any(not isinstance(ref, str) or not ref for ref in evidence):
            raise ValueError("holding_value_evidence_invalid")
        return {
            "currency": currency,
            "value_basis": "observed",
            time_field: copy.deepcopy(observed_at),
            "source_evidence": list(dict.fromkeys([*evidence, source_ref])),
        }

    values: dict[str, Any] = {}
    for field in ("cost_basis_values", "avg_cost_values"):
        if field not in holding:
            continue
        rows = holding[field]
        # One trading-currency observation per value. No implicit FX conversion.
        if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
            raise ValueError("holding_cost_values_invalid")
        row = rows[0]
        if set(row) - {"amount", "currency", "value_basis", "as_of", "source_evidence"}:
            raise ValueError("holding_cost_fields_invalid")
        values[field] = [{**metadata(row, "as_of"), "amount": number(row.get("amount"), nonnegative=True)}]

    if "valuation" in holding:
        row = holding["valuation"]
        allowed = {*VALUATION_NUMBERS, "currency", "value_basis", "observed_at", "source_evidence"}
        if not isinstance(row, Mapping) or set(row) - allowed or not (set(row) & set(VALUATION_NUMBERS)):
            raise ValueError("holding_valuation_fields_invalid")
        valuation = metadata(row, "observed_at")
        for field in VALUATION_NUMBERS:
            if field in row:
                valuation[field] = number(row[field], nonnegative=field in {"last_price", "market_value"})
        values["valuation"] = valuation
    return values
