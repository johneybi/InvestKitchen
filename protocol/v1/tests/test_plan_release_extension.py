from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from protocol.v1.runtime.reference_composition import build_reference_gateway
from protocol.v1.runtime.advisory_plan_store import AdvisoryPlanStore
from protocol.v1.tests.test_advisory_plan_update import ROOT, plan_request
from protocol.v1.tests.test_mcp_write_surface import _grant, _principal, _request


def test_verification_namespace_has_no_writes_to_authoritative_plan_store(tmp_path):
    authority = tmp_path / "authoritative-plans"
    verification = tmp_path / "synthetic-plans"
    namespace = "verification:release-test"
    grant = _grant()
    grant["portfolio_scope"].append(namespace)
    grant["permissions"].append("plan.update")
    gateway = build_reference_gateway(runtime_root=ROOT, manifest=ROOT / "protocol/v1/fixtures/full-reference.instance.json",
        native_store_root=tmp_path / "native", advisory_plan_root=authority,
        plan_verification_root=verification, plan_verification_id=namespace,
        approval_store_root=tmp_path / "approvals", write_principal=_principal(), write_grant=grant)
    preview = gateway.preview_plan_update(plan_request(portfolio_id=namespace))
    result = gateway.apply_plan_update(preview["preview_id"], preview["confirmation"])
    assert result["status"] == "applied" and result["read_back"]["matches"]
    assert not authority.exists()
    assert AdvisoryPlanStore(verification).list_plans()[0]["portfolio_id"] == namespace
    assert gateway.get_plan_history(namespace)["total_events"] == 1
    assert gateway.get_plan_history("portfolio-alpha")["total_events"] == 0
    assert gateway.list_portfolios()["portfolio_ids"] == [namespace]


@pytest.mark.parametrize("namespace", [None, "portfolio-alpha"])
def test_verification_cannot_silently_use_a_personal_namespace(tmp_path, namespace):
    with pytest.raises(ValueError):
        build_reference_gateway(runtime_root=ROOT, manifest=ROOT / "protocol/v1/fixtures/full-reference.instance.json",
            advisory_plan_root=tmp_path / "authority", plan_verification_root=tmp_path / "test",
            plan_verification_id=namespace)


@pytest.mark.parametrize("enabled", [False, True])
def test_pinned_extension_preserves_existing_tools_and_native_plan_flow(tmp_path, enabled):
    base = tmp_path / "prior-runtime"
    shutil.copytree(ROOT / "protocol", base / "protocol", ignore=shutil.ignore_patterns("__pycache__", "tests", "generated"))
    # Represent an older release with no native Plan module or tool definitions.
    for name in ("advisory_plan_store.py", "advisory_plan_update.py"):
        (base / "protocol/v1/runtime" / name).unlink()
    catalog = base / "protocol/v1/transport/tool_catalog.json"
    value = json.loads(catalog.read_text())
    value["tools"] = [row for row in value["tools"] if row["name"] not in {"get_plan_history", "preview_plan_update", "apply_plan_update"}]
    catalog.write_text(json.dumps(value))
    principal, grant = tmp_path / "principal.json", tmp_path / "grant.json"
    principal.write_text(json.dumps(_principal()))
    grant.write_text(json.dumps(_grant()))
    transport = base / "protocol/v1/transport/mcp_stdio.py"
    digest = hashlib.sha256(transport.read_bytes()).hexdigest()
    argv = [sys.executable, str(ROOT / "protocol/v1/deployment/plan_stdio_extension.py"),
            "--base-runtime", str(base), "--base-transport-digest", digest,
            "--runtime-root", str(base), "--manifest", str(base / "protocol/v1/fixtures/full-reference.instance.json"),
            "--native-store-root", str(tmp_path / "native"), "--advisory-state-root", str(tmp_path / "advisory"),
            "--approval-store-root", str(tmp_path / "approvals"), "--write-principal", str(principal), "--write-grant", str(grant)]
    if enabled:
        argv.append("--enable-plan-writes")
    child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    def rpc(method, params=None):
        child.stdin.write(json.dumps(_request(method, params)) + "\n")
        child.stdin.flush()
        response = json.loads(child.stdout.readline())
        assert "error" not in response, response
        return response["result"]
    try:
        names = {row["name"] for row in rpc("tools/list")["tools"]}
        assert {"get_capabilities", "get_decision_history", "get_transactions", "get_plan_history"}.issubset(names)
        assert ("apply_plan_update" in names) is enabled
        if enabled:
            preview = rpc("tools/call", {"name": "preview_plan_update", "arguments": {"update": plan_request()}})["structuredContent"]
            result = rpc("tools/call", {"name": "apply_plan_update", "arguments": {
                "preview_id": preview["preview_id"], "confirmation": preview["confirmation"]}})["structuredContent"]
            assert result["status"] == "applied" and result["read_back"]["matches"]
            rows = rpc("tools/call", {"name": "get_plan_history", "arguments": {"portfolio_id": "portfolio-alpha"}})["structuredContent"]
            assert rows["total_events"] == 1
    finally:
        child.stdin.close()
        child.wait(timeout=10)
        assert child.returncode == 0, child.stderr.read()
    assert hashlib.sha256(transport.read_bytes()).hexdigest() == digest
    bad = argv.copy()
    bad[bad.index("--base-transport-digest") + 1] = "0" * 64
    failed = subprocess.run(bad, input="", text=True, capture_output=True)
    assert failed.returncode == 2 and "configuration error" in failed.stderr
