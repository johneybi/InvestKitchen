from __future__ import annotations

from typing import Any

from protocol.v1.adapters.common import result_envelope
from protocol.v1.runtime.native_write_store import NativeWriteStore
from protocol.v1.runtime.portfolio_checkpoint import (
    CheckpointError,
    PortfolioCheckpointStore,
    project_from_latest_checkpoint,
)


def get_materialized_portfolio_state(
    checkpoint_store: PortfolioCheckpointStore,
    write_store: NativeWriteStore,
    portfolio_id: str,
) -> dict[str, Any]:
    try:
        projection = project_from_latest_checkpoint(
            checkpoint_store,
            write_store,
            portfolio_id=portfolio_id,
        )
    except CheckpointError as exc:
        return result_envelope(
            capability="portfolio.state",
            producer="investkitchen.native.portfolio-materializer",
            status="unavailable",
            data=None,
            authority="derived_calculation",
            freshness="unknown",
            source_mode="local_store",
            warnings=["The native Portfolio checkpoint projection is unavailable."],
            gaps=[{
                "gap_code": "portfolio_checkpoint_unusable",
                "required_capability": "portfolio.state",
                "scope": {"portfolio_id": portfolio_id},
                "reason": exc.code,
                "impact": "Current Portfolio state is not returned from the checkpoint materializer.",
                "recoverable": True,
            }],
            permissions_used=["portfolio.read.positions", "portfolio.read.cash", "transaction.read", "policy.read"],
        )

    materialization = projection["materialization"]
    checkpoint = projection["checkpoint"]
    gaps = list(materialization.get("gaps") or [])
    return result_envelope(
        capability="portfolio.state",
        producer="investkitchen.native.portfolio-materializer",
        status="partial" if gaps else "ok",
        data=materialization["portfolio"],
        authority="derived_calculation",
        generated_at=materialization.get("generated_at"),
        freshness="local",
        source_mode="local_store",
        provenance=[{
            "source_type": "portfolio_checkpoint",
            "source_id": str(checkpoint["checkpoint_id"]),
            "producer": "investkitchen.native.portfolio-materializer",
            "producer_version": "0.1.0",
        }],
        warnings=[str(gap.get("reason") or "") for gap in gaps if isinstance(gap, dict)],
        gaps=gaps,
        permissions_used=["portfolio.read.positions", "portfolio.read.cash", "transaction.read", "policy.read"],
    )
