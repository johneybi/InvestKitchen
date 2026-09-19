from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

from .common import ADAPTER_VERSION, hide_local_locators, logicalize_legacy_evidence_refs, result_envelope


def _load_projector():
    path = Path(__file__).resolve().parents[1] / "tools" / "project_legacy_records.py"
    spec = importlib.util.spec_from_file_location("trademind_protocol_v1_projector", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("protocol v1 legacy projector is unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def get_portfolio_state(workspace: Path, portfolio_id: str) -> dict[str, Any]:
    """Project authoritative legacy portfolio records behind the v1 capability boundary."""
    projector = _load_projector()
    projected = hide_local_locators(
        logicalize_legacy_evidence_refs(projector.project_portfolio(workspace, portfolio_id))
    )
    gaps = list(projected.get("migration_gaps", []))
    status = "partial" if gaps else "ok"
    return result_envelope(
        capability="portfolio.state",
        producer="official.compat.portfolio-legacy",
        status=status,
        data=projected,
        authority="portfolio_fact",
        generated_at=projected["generated_at"],
        freshness="local",
        source_mode="local_store",
        provenance=[
            {
                "source_type": "legacy_portfolio_store",
                "source_id": portfolio_id,
                "producer": "official.compat.portfolio-legacy",
                "producer_version": ADAPTER_VERSION,
            }
        ],
        warnings=[gap["reason"] for gap in gaps],
        gaps=gaps,
        permissions_used=["portfolio.read.positions", "portfolio.read.cash", "transaction.read", "policy.read"],
    )
