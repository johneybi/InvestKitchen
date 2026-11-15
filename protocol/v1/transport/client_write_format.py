"""Optional flat HTTP declarations; canonical write validation stays authoritative."""
from __future__ import annotations

import copy
import hmac
import json
import re
from typing import Any

from protocol.v1.adapters.common import digest
from protocol.v1.transport.mcp_confirmed_writes import WRITE_TOOLS
from protocol.v1.transport.mcp_read_surface import ReadSurfaceError


WRITE_CONTRACT_TOOL = "get_write_contract"
MAX_UPDATE_BYTES = 65536


def json_write_declaration(tool: dict[str, Any]) -> dict[str, Any]:
    if tool["name"] not in WRITE_TOOLS:
        return tool
    result = copy.deepcopy(tool)
    result["description"] += (
        " This connection uses JSON-string input. First use get_write_contract for the canonical schema."
        " Serialize the complete update object into update_json; preserve empty objects, optional fields"
        " and source text exactly. Supply expected_payload_digest only when a trusted prepared digest is available; otherwise omit it, never invent it."
    )
    result["inputSchema"] = {
        "type": "object",
        "properties": {
            "update_json": {"type": "string", "minLength": 2, "maxLength": MAX_UPDATE_BYTES,
                            "description": "JSON serialization of the canonical update object, not an update wrapper."},
            "expected_payload_digest": {
                "type": "string", "pattern": "^[0-9a-f]{64}$",
                "description": "Optional independently prepared SHA-256 of the canonical update. A mismatch rejects staging."},
        },
        "required": ["update_json"], "additionalProperties": False,
    }
    return result


def write_contract_declaration(names: set[str]) -> dict[str, Any]:
    return {
        "name": WRITE_CONTRACT_TOOL,
        "description": "Read the canonical input schema for an authorized preview tool before building update_json. Does not stage or apply a change.",
        "inputSchema": {"type": "object", "properties": {
            "preview_tool": {"type": "string", "enum": sorted(names & WRITE_TOOLS.keys())}},
            "required": ["preview_tool"], "additionalProperties": False},
        "annotations": {"readOnlyHint": True, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": False},
    }


def get_write_contract(writes, token, arguments: dict[str, Any]) -> dict[str, Any]:
    grant = writes.grant(token)
    if (not isinstance(arguments, dict) or set(arguments) != {"preview_tool"}
            or not isinstance(arguments["preview_tool"], str)):
        raise ReadSurfaceError("Invalid tool arguments")
    name = arguments["preview_tool"]
    if name not in WRITE_TOOLS or name not in writes.names(grant):
        raise ReadSurfaceError("Unknown or unavailable write tool")
    tool = next(tool for tool in writes.list_tools(token) if tool["name"] == name)
    return {"status": "ok", "preview_tool": name, "input_format": "json-string",
            "canonical_input_schema": tool["inputSchema"],
            "submission": "Construct an update object conforming to canonical_input_schema.properties.update and serialize that update into update_json, not the schema itself. Do not populate omitted optional fields or replace empty objects.",
            "digest_encoding": "SHA-256 of UTF-8 JSON with sorted object keys, no insignificant whitespace, and unescaped Unicode (InvestKitchen canonical digest).",
            "approval": "separate_authenticated_browser_review"}


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError("Non-finite JSON number")


def decode_write_arguments(arguments: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(arguments, dict) or "update_json" not in arguments:
        raise ReadSurfaceError("JSON-string preview input required; refresh tool definitions and use get_write_contract")
    if set(arguments) - {"update_json", "expected_payload_digest"}:
        raise ReadSurfaceError("Invalid tool arguments")
    raw = arguments["update_json"]
    expected = arguments.get("expected_payload_digest")
    if (not isinstance(raw, str) or ("expected_payload_digest" in arguments
            and (not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None))):
        raise ReadSurfaceError("Invalid tool arguments")
    try:
        if len(raw.encode("utf-8")) > MAX_UPDATE_BYTES:
            raise ValueError("Oversized JSON update")
        update = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        if not isinstance(update, dict):
            raise ValueError("Update must be an object")
        json.dumps(update, allow_nan=False)
        actual = digest(update)
    except (ValueError, TypeError, RecursionError):
        raise ReadSurfaceError("Invalid tool arguments") from None
    if expected is not None and not hmac.compare_digest(actual, expected):
        raise ReadSurfaceError("Update payload digest mismatch")
    return {"update": update}
