from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("script", ["codex-mcp.sh", "container-entrypoint.sh"])
@pytest.mark.parametrize("mode", ["disabled", "enabled"])
def test_plan_launcher_generates_only_explicit_plan_authority(tmp_path, script, mode):
    source = (ROOT / "deploy/synology" / script).read_text()
    blocks = re.findall(r"<<'PY'\n(.*?)\nPY", source, re.S)
    assert len(blocks) == 1
    personal = tmp_path / "synthetic-personal-manifest.json"
    personal.write_text(json.dumps({"portfolio_ids": ["portfolio-alpha"]}))
    principal, grant = tmp_path / "principal.json", tmp_path / "grant.json"
    arguments = [str(personal)]
    if script == "codex-mcp.sh":
        arguments.append(str(ROOT / "protocol/v1/fixtures/full-reference.instance.json"))
    arguments.extend([str(principal), str(grant)])
    result = subprocess.run([sys.executable, "-", *arguments], input=blocks[0], text=True, capture_output=True,
                            env={"PATH": os.environ["PATH"], "INVESTKITCHEN_PLAN_WRITES": mode})
    assert result.returncode == 0, result.stderr
    authority = json.loads(grant.read_text())
    assert authority["portfolio_scope"] == ["portfolio-alpha"]
    assert set(authority["permissions"]) == ({"plan.update", "operation.approve"} if mode == "enabled" else set())
    assert set(authority["tool_allowlist"]) == ({"preview_plan_update", "apply_plan_update"} if mode == "enabled" else set())


def test_gemini_plan_overlay_keeps_other_authority_mounts_read_only(tmp_path):
    env = {"PATH": os.environ["PATH"], "GEMINI_PROJECT": "synthetic-plan-test",
           "GEMINI_HTTP_IMAGE": "investkitchen-http:synthetic", "GEMINI_EDGE_IMAGE": "investkitchen-edge:synthetic",
           "GEMINI_UID": "1000", "GEMINI_GID": "1000", "GEMINI_TRIAL_ROOT": str(tmp_path / "trial"),
           "GEMINI_PLAN_STORE_ROOT": str(tmp_path / "plans"), "GEMINI_PLAN_APPROVAL_ROOT": str(tmp_path / "approvals")}
    (tmp_path / "trial/auth").mkdir(parents=True)
    (tmp_path / "trial/auth/keycloak.env").write_text("SYNTHETIC=true\n")
    result = subprocess.run(["docker", "compose", "-f", str(ROOT / "deploy/gemini/compose.trial.yml"),
                             "-f", str(ROOT / "deploy/gemini/compose.plan.yml"), "config", "--format", "json"],
                            text=True, capture_output=True, env=env)
    assert result.returncode == 0, result.stderr
    runtime = json.loads(result.stdout)["services"]["runtime"]
    mounts = {row["target"]: row for row in runtime["volumes"]}
    assert mounts["/var/lib/investkitchen/state"]["read_only"] is True
    assert mounts["/var/lib/investkitchen/personal"]["read_only"] is True
    assert not mounts["/var/lib/investkitchen/plans"].get("read_only", False)
    assert not mounts["/var/lib/investkitchen/plan-approvals"].get("read_only", False)
    assert runtime["read_only"] is True
