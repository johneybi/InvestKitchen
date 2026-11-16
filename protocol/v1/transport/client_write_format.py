"""Optional flat HTTP declarations; canonical write validation stays authoritative."""
from __future__ import annotations

import copy
import hmac
import json
import re
from typing import Any

from protocol.v1.adapters.common import digest
from protocol.v1.transport.mcp_confirmed_writes import WRITE_TOOLS, UPDATE_TOOLS, DIRECT_WRITE_TOOLS
from protocol.v1.transport.mcp_read_surface import ReadSurfaceError


WRITE_CONTRACT_TOOL = "get_write_contract"
MAX_UPDATE_BYTES = 65536


def note_write_declaration(tool: dict[str, Any]) -> dict[str, Any]:
    if tool["name"] != "register_knowledge":
        return tool
    from protocol.v1.runtime.knowledge_note import NOTE_SCHEMA
    return {**tool, "inputSchema": copy.deepcopy(NOTE_SCHEMA)}


def json_write_declaration(tool: dict[str, Any]) -> dict[str, Any]:
    if tool["name"] == "register_knowledge":
        return note_write_declaration(tool)
    if tool["name"] not in UPDATE_TOOLS or "update" not in tool["inputSchema"].get("properties", {}):
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
    if "target_session_date" in tool["inputSchema"].get("properties", {}):
        result["inputSchema"]["properties"]["target_session_date"] = copy.deepcopy(
            tool["inputSchema"]["properties"]["target_session_date"])
    return result


def write_contract_declaration(names: set[str]) -> dict[str, Any]:
    return {
        "name": WRITE_CONTRACT_TOOL,
        "description": "Read the canonical input schema for an authorized preview tool before building update_json. Does not stage or apply a change.",
        "inputSchema": {"type": "object", "properties": {
            "preview_tool": {"type": "string", "enum": sorted(names & UPDATE_TOOLS)}},
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
    if name not in UPDATE_TOOLS or name not in writes.names(grant):
        raise ReadSurfaceError("Unknown or unavailable write tool")
    tool = next(tool for tool in writes.list_tools(token) if tool["name"] == name)
    if name == "register_knowledge":
        return {"status": "ok", "preview_tool": name, "input_format": "object",
                "canonical_input_schema": note_write_declaration(tool)["inputSchema"],
                "submission": "Submit title, exact source_text and statements directly. Omit unknown optional values. Server creates conservative metadata and IDs. No update_json needed.",
                "approval": "server_granted_append_only"}
    wrapped = "update" in tool["inputSchema"].get("properties", {})
    return {"status": "ok", "preview_tool": name, "input_format": "json-string" if wrapped else "object",
            "canonical_input_schema": tool["inputSchema"],
            "submission": ("Construct an update object conforming to canonical_input_schema.properties.update and serialize that update into update_json, not the schema itself. Do not populate omitted optional fields or replace empty objects. If exposed, target_session_date is a separate submission argument for requested session publication, never a field inside update_json." if wrapped else "Submit arguments matching canonical_input_schema directly; this tool does not use update_json."),
            "digest_encoding": "SHA-256 of UTF-8 JSON with sorted object keys, no insignificant whitespace, and unescaped Unicode (InvestKitchen canonical digest).",
            "approval": ("server_granted_append_only" if name in DIRECT_WRITE_TOOLS else "separate_authenticated_browser_review")}


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
    if set(arguments) - {"update_json", "expected_payload_digest", "target_session_date"}:
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
    result = {"update": update}
    if "target_session_date" in arguments:
        result["target_session_date"] = arguments["target_session_date"]
    return result
