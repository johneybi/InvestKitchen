from decimal import Decimal

import pytest

from protocol.v1.runtime.briefing_analysis import (
    observation_status, position_weight, session_status, simple_moving_average,
)


NOW = "2026-11-05T08:20:00+09:00"


def test_freshness_fails_closed():
    fresh = {"status": "ok", "source": "synthetic", "observed_at": NOW}
    assert observation_status(fresh, now=NOW, max_age_seconds=60) == "ok"
    assert observation_status(None, now=NOW, max_age_seconds=60) == "missing"
    assert observation_status({**fresh, "status": "failed"}, now=NOW, max_age_seconds=60) == "failed"
    assert observation_status({**fresh, "status": "partial"}, now=NOW, max_age_seconds=60) == "partial"
    assert observation_status({**fresh, "observed_at": "2026-11-05T08:18:00+09:00"}, now=NOW, max_age_seconds=60) == "stale"
    assert observation_status({**fresh, "observed_at": "2026-11-05T08:21:00+09:00"}, now=NOW, max_age_seconds=60) == "failed"
    assert observation_status({**fresh, "observed_at": "2026-11-05"}, now=NOW, max_age_seconds=60) == "missing"


def test_session_requires_calendar_evidence_even_on_weekday():
    assert session_status(None, now=NOW, max_age_seconds=60)["session"] == "unknown"
    holiday = {"status": "ok", "source": "synthetic calendar", "observed_at": NOW,
               "session": "closed", "venue": "KRX", "coverage": "regular"}
    assert session_status(holiday, now=NOW, max_age_seconds=60)["session"] == "closed"
    assert session_status({**holiday, "session": "special"}, now=NOW, max_age_seconds=60)["session"] == "special"
    assert session_status({**holiday, "coverage": None}, now=NOW, max_age_seconds=60)["session"] == "unknown"


def test_sma_and_weight_are_deterministic_and_bounded():
    assert simple_moving_average([10, 20, 30], 2) == Decimal("25")
    assert simple_moving_average([10], 2) is None
    assert position_weight("25", "200") == Decimal("12.5")
    assert position_weight(0, 0) is None
    with pytest.raises(ValueError):
        simple_moving_average([1, "NaN"], 2)
    with pytest.raises(ValueError):
        position_weight(201, 200)
