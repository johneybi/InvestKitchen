"""Read committed market editions. The caller supplies an already authorized scope."""

from contextlib import closing
import json
from pathlib import Path
import sqlite3


PAGE_BYTES = 32 * 1024
EDITION_FIELDS = ("edition_id", "market", "trade_date", "kind")
CANDIDATE_FIELDS = ("candidate_id", "name", "status", "reason", "recheck")
SOURCE_FIELDS = ("source_id", "publisher", "title", "known_at", "excerpt", "url", "document_ref")


def _fields(record, keys):
    return {key: record[key] for key in keys}


def _validate_page(offset, limit, maximum):
    if type(offset) is not int or offset < 0:
        raise ValueError("offset must be a non-negative integer")
    if type(limit) is not int or not 1 <= limit <= maximum:
        raise ValueError(f"limit must be between 1 and {maximum}")


def _page(header, key, rows, offset, limit):
    result = {**header, key: [], "next_offset": None}
    if len(_encoded(result)) > PAGE_BYTES:
        return {"status": "unavailable"}
    for index in range(offset, min(len(rows), offset + limit)):
        result[key].append(rows[index])
        result["next_offset"] = index + 1 if index + 1 < len(rows) else None
        if len(_encoded(result)) > PAGE_BYTES:
            result[key].pop()
            if not result[key]:
                return {"status": "unavailable"}
            result["next_offset"] = index
            # The last accepted page was measured with this same next_offset.
            break
    return result


def _encoded(page):
    return json.dumps(page, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class MarketEditionReader:
    """The writer owns validation and recovery. Opening this reader never creates a DB."""

    def __init__(self, database: Path, *, scope_id: str, viewer_editions_url: str):
        if not scope_id:
            raise ValueError("an authorized market edition scope is required")
        self._scope = scope_id
        self._uri = database.resolve().as_uri() + "?mode=ro"
        self._viewer = viewer_editions_url.rstrip("/")

    def _read(self, *, edition_id=None, stream=None):
        columns = "edition_id, market, trade_date, kind, revision, publisher_id, accepted_at, payload"
        query = f"SELECT {columns} FROM editions WHERE scope_id = ?"
        if stream is None:
            query += " AND edition_id = ?"
            parameters = (self._scope, edition_id)
        else:
            query += " AND market = ? AND trade_date = ? AND kind = ? ORDER BY revision DESC LIMIT 1"
            parameters = (self._scope, *stream)
        with closing(sqlite3.connect(self._uri, uri=True)) as db:
            db.row_factory = sqlite3.Row
            row = db.execute(query, parameters).fetchone()
        if row is None:
            return None
        payload = json.loads(row["payload"])
        if any(payload[key] != row[key] for key in EDITION_FIELDS):
            raise ValueError("stored edition identifiers disagree")
        return row, payload

    def get_market_edition(self, *, market=None, trade_date=None, kind=None,
                           edition_id=None, offset=0, limit=15):
        _validate_page(offset, limit, 30)
        stream = (market, trade_date, kind)
        if edition_id is None:
            if not all(stream) or offset:
                raise ValueError("the first page requires a complete stream, later pages an edition ID")
        elif not edition_id or any(value is not None for value in stream):
            raise ValueError("select either a stream or an edition ID")
        try:
            found = self._read(edition_id=edition_id, stream=stream if edition_id is None else None)
            if found is None:
                if edition_id is None:
                    return {"status": "not_published", "market": market, "trade_date": trade_date, "kind": kind}
                return {"status": "not_found"}
            row, payload = found
            report, snapshot = payload["report"], payload["snapshot"]
            candidates = [_fields(item, CANDIDATE_FIELDS) for item in report["candidates"]]
            header = {
                "status": "ok",
                **_fields(row, (*EDITION_FIELDS, "revision", "publisher_id", "accepted_at")),
                **_fields(payload, ("parent_edition_id", "published_at", "source_cutoff_at")),
                **_fields(snapshot, ("snapshot_id", "scope", "coverage", "gaps", "screened_count")),
                **_fields(report, ("title", "summary", "themes")),
                "candidate_count": len(candidates),
                "edition_url": f"{self._viewer}/{row['edition_id']}",
            }
            return _page(header, "candidates", candidates, offset, limit)
        except (sqlite3.Error, ValueError, TypeError, KeyError):
            return {"status": "unavailable"}

    def get_market_candidate_evidence(self, *, edition_id, candidate_id, offset=0, limit=3):
        _validate_page(offset, limit, 8)
        if not edition_id or not candidate_id:
            raise ValueError("an edition ID and candidate ID are required")
        try:
            found = self._read(edition_id=edition_id)
            if found is None:
                return {"status": "not_found"}
            row, payload = found
            candidate = next((item for item in payload["report"]["candidates"]
                              if item["candidate_id"] == candidate_id), None)
            if candidate is None:
                return {"status": "not_found"}
            snapshot = payload["snapshot"]
            sources = {
                source["source_id"]: {key: source[key] for key in SOURCE_FIELDS if key in source}
                for source in snapshot["sources"]
            }
            evidence = [{
                **_fields(item, ("type", "text")),
                "sources": [sources[key] for key in item["source_ids"]],
            } for item in candidate["evidence"]]
            header = {
                "status": "ok",
                **_fields(row, ("edition_id", "revision")),
                "snapshot_id": snapshot["snapshot_id"],
                "source_cutoff_at": payload["source_cutoff_at"],
                **_fields(candidate, ("candidate_id", "name", "reason", "recheck")),
                "candidate_status": candidate["status"],
                "evidence_total": len(evidence),
                "evidence_gap": candidate.get("evidence_gap"),
            }
            return _page(header, "evidence", evidence, offset, limit)
        except (sqlite3.Error, ValueError, TypeError, KeyError):
            return {"status": "unavailable"}
