"""Pure, bounded calculations for stock analysis and briefing consumers.

This module does not fetch data, infer a trading calendar, generate advice, or
persist a briefing. Callers must supply observed data and its provenance.
"""

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence


def _time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(timezone.utc)


def observation_status(observation: Mapping[str, Any] | None, *, now: str, max_age_seconds: int) -> str:
    """Classify an observation without substituting cached or remembered data."""
    current = _time(now)
    if max_age_seconds < 0:
        raise ValueError("max_age_seconds must be nonnegative")
    if observation is None:
        return "missing"
    if observation.get("status") == "failed":
        return "failed"
    if observation.get("status") not in {"ok", "partial"} or not observation.get("source") or not observation.get("observed_at"):
        return "missing"
    try:
        observed = _time(observation["observed_at"])
    except (TypeError, ValueError):
        return "missing"
    if observed > current:
        return "failed"
    if current - observed > timedelta(seconds=max_age_seconds):
        return "stale"
    return observation["status"]


def _number(value: Any) -> Decimal:
    if isinstance(value, bool):
        raise ValueError("boolean is not a number")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid number") from None
    if not result.is_finite():
        raise ValueError("number must be finite")
    return result


def simple_moving_average(closes: Sequence[Any], window: int) -> Decimal | None:
    """Return an SMA of the most recent closes; insufficient history is unknown."""
    if not 1 <= window <= 250 or len(closes) > 1000:
        raise ValueError("series or window outside bounds")
    if len(closes) < window:
        return None
    values = [_number(value) for value in closes[-window:]]
    if any(value <= 0 for value in values):
        raise ValueError("closing prices must be positive")
    return sum(values, Decimal(0)) / window


def position_weight(value: Any, total: Any) -> Decimal | None:
    """Percentage of a consistently valued portfolio, if the total is usable."""
    numerator, denominator = _number(value), _number(total)
    if numerator < 0 or denominator < 0 or numerator > denominator:
        raise ValueError("inconsistent portfolio valuation")
    if denominator == 0:
        return None
    return numerator * 100 / denominator


def session_status(calendar: Mapping[str, Any] | None, *, now: str, max_age_seconds: int) -> dict[str, Any]:
    """Return only a supplied, current calendar result; never guess from weekday."""
    status = observation_status(calendar, now=now, max_age_seconds=max_age_seconds)
    if status != "ok":
        return {"status": status, "session": "unknown"}
    session = calendar.get("session")
    if (session not in {"closed", "regular", "pre_regular", "post_regular", "special"}
            or not calendar.get("venue") or not calendar.get("coverage")):
        return {"status": "missing", "session": "unknown"}
    return {"status": "ok", "session": session, "source": calendar["source"],
            "observed_at": calendar["observed_at"], "venue": calendar.get("venue"),
            "coverage": calendar.get("coverage")}
