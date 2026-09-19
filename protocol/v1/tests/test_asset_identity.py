from __future__ import annotations

from protocol.v1.runtime.asset_identity import (
    asset_identity_conflict,
    asset_identity_issue,
    canonical_asset_identity,
)


def _market(**overrides):
    value = {
        "asset_type": "stock",
        "symbol": "000001",
        "venue": "KRX",
        "currency": "KRW",
        "display_name": "Synthetic Security",
        "provider_refs": {},
    }
    value.update(overrides)
    return value


def test_market_identity_is_asset_type_symbol_venue_currency_even_with_asset_id() -> None:
    base = _market()
    enriched = _market(asset_id="asset:synthetic")
    assert canonical_asset_identity(base) == (
        "market_identity",
        "stock",
        "000001",
        "KRX",
        "KRW",
    )
    assert canonical_asset_identity(enriched) == canonical_asset_identity(base)


def test_market_identity_requires_verified_symbol_and_currency() -> None:
    assert canonical_asset_identity(_market(symbol=None)) is None
    assert canonical_asset_identity(_market(currency=None)) is None
    assert asset_identity_issue(_market(symbol=None))["reason"] == "market_identity_incomplete"


def test_non_market_provider_identity_ignores_display_name() -> None:
    first = {
        "asset_type": "fund",
        "symbol": None,
        "venue": None,
        "currency": "KRW",
        "display_name": "Synthetic Fund A",
        "provider_refs": {"fund_code": "FUND-001"},
    }
    renamed = {**first, "display_name": "Synthetic Fund B"}
    assert canonical_asset_identity(first) == ("provider_identity", "fund", "fund_code", "FUND-001")
    assert canonical_asset_identity(renamed) == canonical_asset_identity(first)


def test_non_market_multiple_provider_ids_fail_closed_without_asset_id() -> None:
    value = {
        "asset_type": "other",
        "symbol": None,
        "venue": None,
        "currency": "KRW",
        "display_name": "Synthetic Product",
        "provider_refs": {"product_code": "P-1", "provider_asset_id": "A-1"},
    }
    assert canonical_asset_identity(value) is None
    assert asset_identity_issue(value)["reason"] == "non_market_provider_identity_ambiguous"
    value["asset_id"] = "asset:stable-product"
    assert canonical_asset_identity(value) == ("asset_id", "asset:stable-product")


def test_symbol_less_unknown_asset_stays_unresolved_but_legacy_symbol_is_market_compatible() -> None:
    unknown = {
        "asset_type": "unknown",
        "symbol": None,
        "venue": None,
        "currency": "KRW",
        "display_name": "Synthetic Unknown",
        "provider_refs": {"product_code": "P-1"},
    }
    assert canonical_asset_identity(unknown) is None
    assert canonical_asset_identity({**unknown, "symbol": "000001"}) == (
        "market_identity",
        "unknown",
        "000001",
        "<none>",
        "KRW",
    )


def test_asset_id_conflict_ignores_name_but_rejects_conflicting_provider_facts() -> None:
    left = {
        "asset_id": "asset:stable-product",
        "asset_type": "fund",
        "symbol": None,
        "venue": None,
        "currency": "KRW",
        "display_name": "Name A",
        "provider_refs": {"fund_code": "F-1"},
    }
    renamed = {**left, "display_name": "Name B"}
    conflict = {**renamed, "provider_refs": {"fund_code": "F-2"}}
    assert asset_identity_conflict(left, renamed) is False
    assert asset_identity_conflict(left, conflict) is True
