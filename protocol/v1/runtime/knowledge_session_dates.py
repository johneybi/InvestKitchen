"""Validate explicit publication intent without rewriting source or claim dates."""
from datetime import date, datetime
import re
from zoneinfo import ZoneInfo

from protocol.v1.runtime.native_knowledge_store import KnowledgeRejected


def session_day(point):
    raw = point.get("value") if isinstance(point, dict) else None
    if not isinstance(raw, str):
        return None
    try:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
            return date.fromisoformat(raw).isoformat()
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        return value.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat() if value.tzinfo else None
    except ValueError:
        return None


def review_session_dates(update, target_session_date=None):
    claims = [c for c in update["claims"] if c["claim_type"].casefold() == "trading_material"
              and c["registration_state"] == "canonical" and c["truth_status"] != "contradicted"
              and c["applicability_status"] in {"applicable", "conditional"}]
    dates = [session_day(c.get("effective_at")) for c in claims]
    if target_session_date is not None:
        if (not isinstance(target_session_date, str)
                or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", target_session_date)
                or session_day({"value": target_session_date}) != target_session_date):
            raise KnowledgeRejected("knowledge_target_session_date_invalid")
        if not claims:
            raise KnowledgeRejected("knowledge_target_session_has_no_trading_claims")
        if any(value != target_session_date for value in dates):
            raise KnowledgeRejected("knowledge_target_session_date_mismatch")
    return {
        "target_session_date": target_session_date,
        "trading_claim_count": len(claims),
        "claim_effective_dates": sorted({d for d in dates if d}),
        "missing_effective_date_count": dates.count(None),
        "source_publication_dates": sorted({d for e in update["evidence"]
                                            if (d := session_day(e.get("published_at")))}),
        "alignment_checked": target_session_date is not None,
        "scope": "date_alignment_only_not_market_calendar_or_symbol_validation",
        "session_published": False,
    }
