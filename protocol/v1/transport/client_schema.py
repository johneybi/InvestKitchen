"""Add portable type/enum hints to HTTP tool declarations.

Some MCP consumers discard JSON Schema ``const`` when converting declarations
to function parameters. Explicitly typed enums keep fixed values visible. Keep
original constraints and references; validation uses the unmodified router schema.
"""
from __future__ import annotations

import copy
from typing import Any


def _scalar_type(value: Any) -> str | None:
    if isinstance(value, str):
        return "string"
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if value is None:
        return "null"
    return None


def client_tool_schema(schema: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(schema)

    def visit(node: Any) -> None:
        if not isinstance(node, dict):
            return
        if "const" in node and _scalar_type(node["const"]) is not None:
            node.setdefault("enum", [copy.deepcopy(node["const"])])
        values = node.get("enum", [])
        types = {_scalar_type(value) for value in values}
        if values and len(types) == 1 and None not in types:
            node.setdefault("type", next(iter(types)))
        # Traverse schema positions only; examples/defaults may contain arbitrary
        # user-shaped objects with keys such as "const", "enum" or "properties".
        for key in ("properties", "patternProperties", "$defs", "definitions", "dependentSchemas"):
            for child in node.get(key, {}).values():
                visit(child)
        for key in ("items", "additionalProperties", "contains", "not", "if", "then", "else",
                    "propertyNames", "unevaluatedItems", "unevaluatedProperties"):
            visit(node.get(key))
        for key in ("allOf", "anyOf", "oneOf", "prefixItems"):
            for child in node.get(key, []):
                visit(child)

    visit(result)
    return result
