"""Distribution and integrity checks, not a claim about LLM behavior."""
import importlib.util
import json
from pathlib import Path
import shutil
import zipfile

import pytest

from protocol.v1.workflows.protocols import PROTOCOL_VERSION

ROOT = Path(__file__).resolve().parents[3]
SPEC = importlib.util.spec_from_file_location("client_build", ROOT / "scripts/build_client_instructions.py")
builder = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(builder)


@pytest.fixture
def source_copy(tmp_path):
    for relative in ("client-instructions", "protocol/v1/workflows"):
        shutil.copytree(ROOT / relative, tmp_path / relative)
    return tmp_path


def test_committed_deliveries_are_current_and_match_server_version():
    assert builder.check(ROOT) == []
    release = json.loads((ROOT / "client-instructions/release.json").read_text())
    assert release["methodology_version"] == PROTOCOL_VERSION
    assert release["host_installation_verified"] is False
    assert release["web_behavior_verified"] is False


def test_both_clients_contain_identical_core_and_methodology():
    outputs = builder.render(ROOT)
    core = (ROOT / "client-instructions/core.ko.md").read_bytes()
    assert core in outputs[builder.CHATGPT / "project-instructions.ko.md"]
    assert core in outputs[builder.GEMINI / "SKILL.md"]
    for path, raw in outputs.items():
        if path.is_relative_to(builder.CHATGPT / "project-sources"):
            assert raw == outputs[builder.GEMINI / "references" / path.name]


def test_uploaded_bundle_contains_only_allowlisted_artifacts_and_is_reproducible(tmp_path):
    outputs = builder.render(ROOT)
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    builder.write_zip(first, outputs)
    builder.write_zip(second, outputs)
    assert first.read_bytes() == second.read_bytes()
    expected = {"investkitchen/" + path.relative_to(builder.GEMINI).as_posix(): raw
                for path, raw in outputs.items() if path.is_relative_to(builder.GEMINI)}
    with zipfile.ZipFile(first) as archive:
        assert set(archive.namelist()) == set(expected)
        assert "investkitchen/SKILL.md" in archive.namelist()
        for name, raw in expected.items():
            assert archive.read(name) == raw


def test_drift_and_obsolete_references_are_detected(source_copy):
    for path, raw in builder.render(source_copy).items():
        (source_copy / path).parent.mkdir(parents=True, exist_ok=True)
        (source_copy / path).write_bytes(raw)
    assert not builder.check(source_copy)
    changed = builder.GEMINI / "SKILL.md"
    stale = builder.GEMINI / "references/old-policy.md"
    (source_copy / changed).write_text("manually changed installed copy")
    (source_copy / stale).write_text("obsolete")
    assert builder.check(source_copy) == sorted([str(changed), str(stale)])


def test_invalid_methodology_is_never_packaged(source_copy):
    (source_copy / "protocol/v1/workflows/sources/Operating_Contract.ko.md").write_text("changed")
    with pytest.raises(ValueError, match="integrity mismatch"):
        builder.render(source_copy)


def test_release_cannot_pull_sources_from_outside_product_tree(source_copy, tmp_path):
    release_path = source_copy / "client-instructions/release.json"
    release = json.loads(release_path.read_text())
    release["shared_source"] = "../private-instructions.md"
    release_path.write_text(json.dumps(release))
    with pytest.raises(ValueError, match="inside the product tree"):
        builder.render(source_copy)
