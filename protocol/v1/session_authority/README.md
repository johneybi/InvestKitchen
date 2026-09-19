# InvestKitchen native session authority

This directory is the personal-use producer for the existing `SessionBrief` v1,
`SessionBriefAmendment` v1, and `MaterialContext` v1 trading-runtime exchange.
It is intentionally isolated from Portfolio authority and does not import or
execute anything from the legacy `trademind-framework` repository.

## Wire identity

- `producer_id`: `investkitchen`
- `producer_release`: `session-authority-v1`
- package compatibility ID: `trademind-session-exchange`
- contract version: `1.0`
- authority stream: `authority:<YYYY-MM-DD>`
- supporting-context stream: `context:<YYYY-MM-DD>`

The `trademind-session-exchange` package name and the copied v1 schema IDs are
compatibility protocol identifiers. They do not identify the current product.
The producer emits `session_brief.v1`, `session_amendment.v1`, and
`material_context.v1`; it does not recreate the legacy knowledge-registration
or material-extraction pipeline.

## Boundary

Callers provide already structured, source-authorized v1 payloads. The producer
validates the payload, enforces the amendment base/predecessor and record hash
the immutable event pairs into a date-scoped neutral exchange directory.

`MaterialContext` is chained independently under `context:<date>`. It preserves
the exact legacy v1 shape: the top-level payload and every material item carry
`candidate_authority: "NONE"`. Context may support the runtime's material gate,
but it cannot add candidates or mutate the SessionBrief authority universe.
Each context event is causally bound to an already validated current-session
authority event while its predecessor hash advances only within the context
stream.

Publication is append-only per stream for an existing InvestKitchen generation:
old event and schema bytes must remain exact, while later valid authority or
context events may extend their own stream and replace only `index.json` /
`exchange.json` metadata.
An exchange created by a different producer identity is rejected rather than
mixed into the same stream.

Raw local filesystem locations and secret-like markers are rejected when they
appear in payload bytes. Publication results intentionally report only session
and event identities, never the host output path.

## Programmatic use

```python
from pathlib import Path

from protocol.v1.session_authority import SessionAuthorityProducer

producer = SessionAuthorityProducer("2026-09-17")
brief_event = producer.append_brief(brief_payload, occurred_at="2026-09-16T21:25:00Z")
producer.append_context(material_context_payload, occurred_at="2026-09-16T21:25:01Z")

# A later amendment must bind to brief_event and the current predecessor.
producer.append_amendment(amendment_payload, occurred_at="2026-09-16T22:10:00Z")
producer.publish_exchange(Path("neutral-session-exchange"), published_at="2026-09-16T22:10:00Z")
```

The caller is responsible for constructing source evidence and candidate
records from InvestKitchen's own native ingestion pipeline. This module is only
the final session-authority contract producer.
