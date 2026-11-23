from copy import deepcopy
import json
from pathlib import Path
import re
import sqlite3

import pytest

from protocol.v1.runtime.market_edition_reader import MarketEditionReader, PAGE_BYTES


CONTRACT = Path(__file__).resolve().parents[3] / "docs/preopen-market-edition-contract.ko.md"
STREAM = {"market": "KR", "trade_date": "2026-11-20", "kind": "preopen"}
VIEWER = "https://market.example.org/editions"


@pytest.fixture
def examples():
    blocks = re.findall(r"```json\n(.*?)\n```", CONTRACT.read_text(encoding="utf-8"), re.S)
    request, receipt, summary, detail = map(json.loads, blocks)
    return request, receipt, summary, detail


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "market editions.sqlite"
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE editions (
        scope_id TEXT, edition_id TEXT, market TEXT, trade_date TEXT, kind TEXT, revision INTEGER,
        publisher_id TEXT, accepted_at TEXT, payload BLOB, PRIMARY KEY(scope_id, edition_id),
        UNIQUE(scope_id, market, trade_date, kind, revision))""")
    try:
        yield db, MarketEditionReader(path, scope_id="research", viewer_editions_url=VIEWER)
    finally:
        db.close()


def stage(db, payload, receipt, revision=1, scope="research"):
    db.execute("INSERT INTO editions VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", (
        scope, payload["edition_id"], payload["market"], payload["trade_date"], payload["kind"],
        revision, receipt["publisher_id"], receipt["accepted_at"],
        json.dumps(payload, ensure_ascii=False).encode("utf-8"),
    ))


def test_documented_summary_and_detail(database, examples):
    db, reader = database
    request, receipt, summary, detail = examples
    stage(db, request, receipt)
    db.commit()
    assert reader.get_market_edition(**STREAM) == summary
    assert reader.get_market_edition(edition_id=summary["edition_id"]) == summary
    assert reader.get_market_candidate_evidence(
        edition_id=summary["edition_id"], candidate_id=detail["candidate_id"],
    ) == detail


def test_correction_keeps_earlier_source_and_candidate_page(database, examples):
    db, reader = database
    first, receipt, _, _ = examples
    original = first["report"]["candidates"][0]
    second = {**original, "candidate_id": "KRX:000002", "name": "합성종목 B",
              "evidence": [], "evidence_gap": "최초 공시 버전 미확인"}
    first["report"]["candidates"].append(second)
    stage(db, first, receipt)
    db.commit()
    opened = reader.get_market_edition(**STREAM, limit=1)
    pinned_id = opened["edition_id"]
    ticker = opened["candidates"][0]["candidate_id"]

    corrected = deepcopy(first)
    corrected["parent_edition_id"] = pinned_id
    corrected["edition_id"] = "10000000-0000-4000-8000-000000000002"
    corrected["snapshot"]["snapshot_id"] = "20000000-0000-4000-8000-000000000002"
    corrected["snapshot"]["sources"][0]["excerpt"] = "수정판의 새 발췌"
    corrected["report"]["candidates"] = [{**original, "reason": "수정판 선정 이유"}]
    stage(db, corrected, receipt, revision=2)
    assert reader.get_market_edition(**STREAM)["edition_id"] == pinned_id
    db.commit()

    latest = reader.get_market_edition(**STREAM)
    assert latest["edition_id"] == corrected["edition_id"]
    assert latest["candidates"][0]["reason"] == "수정판 선정 이유"
    assert reader.get_market_candidate_evidence(
        edition_id=latest["edition_id"], candidate_id=ticker,
    )["evidence"][0]["sources"][0]["excerpt"] == "수정판의 새 발췌"
    pinned = reader.get_market_candidate_evidence(edition_id=pinned_id, candidate_id=ticker)
    assert pinned["reason"] == original["reason"]
    assert pinned["evidence"][0]["sources"] == first["snapshot"]["sources"]
    page = reader.get_market_edition(edition_id=pinned_id, offset=opened["next_offset"])
    assert [row["candidate_id"] for row in page["candidates"]] == [second["candidate_id"]]
    assert page["next_offset"] is None
    gap = reader.get_market_candidate_evidence(edition_id=pinned_id, candidate_id=second["candidate_id"])
    assert (gap["evidence"], gap["evidence_gap"]) == ([], second["evidence_gap"])
    assert reader.get_market_candidate_evidence(
        edition_id=latest["edition_id"], candidate_id=second["candidate_id"],
    ) == {"status": "not_found"}


def test_scope_and_missing_date(database, examples):
    db, reader = database
    request, receipt, _, detail = examples
    stage(db, request, receipt, scope="other")
    db.commit()
    assert reader.get_market_edition(**STREAM) == {"status": "not_published", **STREAM}
    assert reader.get_market_edition(edition_id=request["edition_id"]) == {"status": "not_found"}
    assert reader.get_market_candidate_evidence(
        edition_id=request["edition_id"], candidate_id=detail["candidate_id"],
    ) == {"status": "not_found"}
    stage(db, request, receipt)
    db.commit()
    next_day = {**STREAM, "trade_date": "2026-11-21"}
    assert reader.get_market_edition(**next_day) == {"status": "not_published", **next_day}
    with pytest.raises(ValueError):
        reader.get_market_edition(**STREAM, offset=1)
    with pytest.raises(ValueError):
        reader.get_market_edition(**STREAM, edition_id=request["edition_id"])


def test_read_only_database_unavailable(tmp_path):
    path = tmp_path / "missing.sqlite"
    reader = MarketEditionReader(path, scope_id="research", viewer_editions_url=VIEWER)
    assert reader.get_market_edition(**STREAM) == {"status": "unavailable"}
    assert not path.exists()
    path.write_bytes(b"damaged synthetic database")
    assert reader.get_market_edition(**STREAM) == {"status": "unavailable"}


def test_pagination_measures_utf8_and_returns_whole_candidates(database, examples):
    db, reader = database
    request, receipt, _, _ = examples
    original = request["report"]["candidates"][0]
    candidates = [{**original, "candidate_id": f"KRX:{i:06d}", "reason": "가"*600, "recheck": "나"*600}
                  for i in range(15)]
    request["report"]["candidates"] = candidates
    stage(db, request, receipt)
    db.commit()
    page = reader.get_market_edition(**STREAM)
    assert page["next_offset"] is not None
    assert page["candidate_count"] == 15 and page["coverage"] == "partial"
    assert page["gaps"] == request["snapshot"]["gaps"]
    seen = []
    while True:
        assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode()) <= PAGE_BYTES
        seen.extend(page["candidates"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] == len(seen)
        page = reader.get_market_edition(edition_id=page["edition_id"], offset=len(seen))
    assert [item["candidate_id"] for item in seen] == [item["candidate_id"] for item in candidates]
    assert all(item["reason"] == "가"*600 and item["recheck"] == "나"*600 for item in seen)


def test_evidence_page_keeps_citations_together_and_reports_partial_gap(database, examples):
    db, reader = database
    request, receipt, _, _ = examples
    source = request["snapshot"]["sources"][0]
    sources = [{**source, "source_id": f"s{i}", "excerpt": "가" * 600} for i in range(4)]
    request["snapshot"]["sources"] = sources
    candidate = request["report"]["candidates"][0]
    candidate["evidence"] = [
        {"type": "fact", "text": f"합성 근거 {i}", "source_ids": [s["source_id"] for s in sources]}
        for i in range(6)
    ]
    candidate["evidence_gap"] = "별도 공시의 최초 버전 미확인"
    stage(db, request, receipt)
    db.commit()
    selector = {"edition_id": request["edition_id"], "candidate_id": candidate["candidate_id"]}
    first = reader.get_market_candidate_evidence(**selector, limit=8)
    offset = first["next_offset"]
    assert 0 < offset < 6
    last = reader.get_market_candidate_evidence(**selector, offset=offset, limit=8)
    assert last["next_offset"] is None
    for page in (first, last):
        assert len(json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode()) <= PAGE_BYTES
        assert page["evidence_total"] == 6 and page["evidence_gap"] == candidate["evidence_gap"]
    rows = first["evidence"] + last["evidence"]
    assert [item["text"] for item in rows] == [item["text"] for item in candidate["evidence"]]
    assert all(item["sources"] == sources for item in rows)
