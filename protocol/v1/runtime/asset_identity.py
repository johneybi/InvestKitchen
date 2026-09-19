from __future__ import annotations

from typing import Any


CanonicalAssetIdentity = tuple[str, ...]


_MARKET_ASSET_TYPES = {"stock", "etf", "index", "bond", "commodity", "crypto"}
_STABLE_PROVIDER_REF_KEYS = (
    "product_code",
    "fund_code",
    "provider_product_id",
    "provider_asset_id",
)


def _text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def canonical_provider_ref(asset: Any) -> tuple[str, str] | None:
    """Return one explicit stable provider reference for a non-market asset.

    Only one provider reference participates in the canonical key. Extra
    presentation/provider metadata therefore cannot change identity merely by
    being added to an otherwise unchanged observation.
    """

    if not isinstance(asset, dict):
        return None
    provider_refs = asset.get("provider_refs")
    if not isinstance(provider_refs, dict):
        return None
    refs = [
        (key, value)
        for key in _STABLE_PROVIDER_REF_KEYS
        if (value := _text(provider_refs.get(key))) is not None
    ]
    # Without an explicit internal asset_id there is no trustworthy way to know
    # whether two different provider identifiers are aliases for the same
    # product. Treat multiple candidates as ambiguous rather than letting key
    # priority or later enrichment silently change identity.
    return refs[0] if len(refs) == 1 else None


def canonical_asset_identity(asset: Any) -> CanonicalAssetIdentity | None:
    """Return a stable identity without deriving one from presentation fields.

    Existing internal ``asset_id`` remains authoritative for compatibility. Market
    assets use symbol/venue/currency. Explicit non-market assets may use one or
    more provider-supplied stable product references. ``display_name`` is never
    identity material.
    """

    if not isinstance(asset, dict):
        return None

    asset_type = _text(asset.get("asset_type")) or "unknown"
    symbol = _text(asset.get("symbol"))
    currency = _text(asset.get("currency"))
    venue = asset.get("venue")
    if asset_type in _MARKET_ASSET_TYPES or (asset_type == "unknown" and symbol is not None):
        if symbol is None:
            return None
        if currency is None or (venue is not None and not isinstance(venue, str)):
            return None
        normalized_venue = _text(venue) if isinstance(venue, str) else None
        return (
            "market_identity",
            asset_type,
            symbol,
            normalized_venue or "<none>",
            currency,
        )

    # Existing opaque IDs remain authoritative for explicitly non-market assets.
    # Market assets intentionally do not switch key merely because an asset_id is
    # later attached to the same verified symbol/venue/currency identity.
    asset_id = _text(asset.get("asset_id"))
    if asset_id is not None:
        return ("asset_id", asset_id)

    # Symbol-less unknown assets are never silently interpreted as provider
    # products. Legacy rows that already carry an explicit symbol+currency are
    # allowed through the market identity path above without inferring a type.
    if asset_type == "unknown":
        return None

    provider_ref = canonical_provider_ref(asset)
    if provider_ref is None:
        return None
    provider_key, provider_value = provider_ref
    return ("provider_identity", asset_type, provider_key, provider_value)


def asset_identity_conflict(left: Any, right: Any) -> bool:
    """Return whether two assets with the same canonical key conflict safely.

    Presentation-only changes such as ``display_name`` are intentionally ignored.
    The check matters most for legacy ``asset_id`` rows where the opaque ID is
    authoritative but contradictory market/provider facts must still fail closed.
    """

    left_identity = canonical_asset_identity(left)
    right_identity = canonical_asset_identity(right)
    if left_identity is None or right_identity is None or left_identity != right_identity:
        return False
    if not isinstance(left, dict) or not isinstance(right, dict):
        return True

    for field in ("asset_type", "symbol", "venue", "currency"):
        left_value = left.get(field)
        right_value = right.get(field)
        if left_value not in (None, "", "unknown") and right_value not in (None, "", "unknown"):
            if left_value != right_value:
                return True

    left_refs = left.get("provider_refs") if isinstance(left.get("provider_refs"), dict) else {}
    right_refs = right.get("provider_refs") if isinstance(right.get("provider_refs"), dict) else {}
    for key in _STABLE_PROVIDER_REF_KEYS:
        left_value = _text(left_refs.get(key))
        right_value = _text(right_refs.get(key))
        if left_value is not None and right_value is not None and left_value != right_value:
            return True
    return False


def asset_identity_issue(asset: Any) -> dict[str, Any]:
    """Describe why an asset has no canonical identity using safe field names."""

    if not isinstance(asset, dict):
        return {
            "missing_fields": ["asset"],
            "reason": "asset_not_object",
            "repair_hint": "Provide a structured asset with a stable market or provider identity.",
        }

    asset_type = _text(asset.get("asset_type")) or "unknown"
    symbol = _text(asset.get("symbol"))
    currency = _text(asset.get("currency"))
    venue = asset.get("venue")

    if asset_type in _MARKET_ASSET_TYPES or (asset_type == "unknown" and symbol is not None):
        missing: list[str] = []
        if symbol is None:
            missing.append("asset.symbol")
        if currency is None:
            missing.append("asset.currency")
        if venue is not None and not isinstance(venue, str):
            missing.append("asset.venue")
        return {
            "missing_fields": missing,
            "reason": "market_identity_incomplete",
            "repair_hint": "Provide the verified market symbol/venue/currency; do not infer a symbol from the display name.",
        }

    if asset_type == "unknown":
        return {
            "missing_fields": ["asset.asset_id", "asset.asset_type", "asset.provider_refs"],
            "reason": "asset_kind_and_identity_unresolved",
            "repair_hint": "Provide a verified provider product/fund code or convert the asset to an explicit non-market type with a stable identity.",
        }

    provider_refs = asset.get("provider_refs")
    stable_refs = [
        key
        for key in _STABLE_PROVIDER_REF_KEYS
        if isinstance(provider_refs, dict) and _text(provider_refs.get(key)) is not None
    ]
    if len(stable_refs) > 1:
        return {
            "missing_fields": ["asset.asset_id"],
            "reason": "non_market_provider_identity_ambiguous",
            "repair_hint": "Provide one canonical provider product/fund identifier, or bind multiple provider references behind one verified stable asset_id.",
        }
    if canonical_provider_ref(asset) is None:
        return {
            "missing_fields": [
                "asset.asset_id",
                "asset.provider_refs.provider_asset_id",
                "asset.provider_refs.provider_product_id",
                "asset.provider_refs.product_code",
                "asset.provider_refs.fund_code",
            ],
            "reason": "non_market_identity_incomplete",
            "repair_hint": "Provide a verified provider product/fund code or a stable asset_id backed by provider evidence.",
        }

    return {
        "missing_fields": ["asset.identity"],
        "reason": "asset_identity_invalid",
        "repair_hint": "Repair the asset identity using verified provider or market data.",
    }


def public_asset_identity(identity: CanonicalAssetIdentity) -> dict[str, Any]:
    if identity[0] == "asset_id":
        return {"asset_id": identity[1]}
    if identity[0] == "provider_identity":
        return {"asset_type": identity[1], "provider_refs": {identity[2]: identity[3]}}
    return {
        "asset_type": identity[1],
        "symbol": identity[2],
        "venue": None if identity[3] == "<none>" else identity[3],
        "currency": identity[4],
    }


__all__ = [
    "CanonicalAssetIdentity",
    "asset_identity_conflict",
    "asset_identity_issue",
    "canonical_asset_identity",
    "canonical_provider_ref",
    "public_asset_identity",
]
