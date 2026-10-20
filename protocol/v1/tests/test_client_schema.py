from __future__ import annotations

import copy

import pytest
from jsonschema import Draft202012Validator

from protocol.v1.transport.client_schema import client_tool_schema
from protocol.v1.transport.stdio_rpc import load_tool_catalog


def test_all_catalog_schemas_keep_constraints_and_make_scalar_choices_typed():
    def check(original, portable):
        if isinstance(original, dict):
            for key, value in original.items():
                check(value, portable[key])
            if "const" in original and isinstance(original["const"], (str, int)):
                assert portable["enum"] == [original["const"]]
                assert portable["type"] in {"string", "integer", "number"}
        elif isinstance(original, list):
            assert len(original) == len(portable)
            for left, right in zip(original, portable):
                check(left, right)
        else:
            assert portable == original

    for tool in load_tool_catalog()["tools"]:
        original = tool["input_schema"]
        before = copy.deepcopy(original)
        portable = client_tool_schema(original)
        Draft202012Validator.check_schema(portable)
        check(original, portable)
        assert original == before
        assert client_tool_schema(portable) == portable


@pytest.mark.parametrize("value,kind", [("knowledge.commit", "string"), (1, "number"),
                                        (True, "boolean"), (None, "null")])
def test_scalar_choices_survive_a_consumer_that_discards_const(value, kind):
    original = {"type": "object", "properties": {"fixed": {"const": value}}, "required": ["fixed"]}
    portable = client_tool_schema(original)
    field = portable["properties"]["fixed"]
    assert field["type"] == kind and field["enum"] == [value]
    del field["const"]
    assert Draft202012Validator(portable).is_valid({"fixed": value})
    assert not Draft202012Validator(portable).is_valid({"fixed": "wrong"})


def test_schema_hints_do_not_modify_annotations_or_mixed_choices():
    schema = {"properties": {"x": {"enum": ["a", "b"]}, "mixed": {"enum": [1, "a"]}},
              "$defs": {"fixed": {"const": "nested"}},
              "examples": [{"const": "data"}], "default": {"enum": ["data"]}}
    portable = client_tool_schema(schema)
    assert portable["properties"]["x"]["type"] == "string"
    assert portable["$defs"]["fixed"]["enum"] == ["nested"]
    for key in ["examples", "default"]:
        assert portable[key] == schema[key]
    assert portable["properties"]["mixed"] == schema["properties"]["mixed"]
