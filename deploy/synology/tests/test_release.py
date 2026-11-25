"""Exercise the actual release CLI using synthetic GitHub data and a Docker CLI double."""

import importlib.util
import json
import os
from pathlib import Path
import shutil
import sys
from urllib.parse import parse_qs, urlsplit
from zipfile import ZipFile

import pytest

SCRIPTS = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS))
from release_common import COMPOSE_FILES, digest, image_ref  # noqa: E402
from release_github import GitHub  # noqa: E402

spec = importlib.util.spec_from_file_location("release_cli", SCRIPTS / "deploy-release.py")
cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cli)

REPO = "fixture/investkitchen"
BASE = "1" * 40
NEXT = "2" * 40
CONTROLLER = "3" * 40
FINAL = "4" * 40


class Actions:
    def __init__(self, root, monkeypatch):
        self.root = root
        self.runs = []
        self.artifacts = {}
        self.downloads = []
        self.reads = []
        self.reject_download = None
        self.comparison = "ahead"
        monkeypatch.setattr(GitHub, "read", lambda _, path: self.read(path))
        monkeypatch.setattr(GitHub, "download", lambda _, *args: self.download(*args))

    def read(self, path):
        self.reads.append(path)
        if path.startswith("actions/workflows/") and "/runs?" in path:
            query = parse_qs(urlsplit(path).query)
            page = int(query["page"][0])
            size = int(query["per_page"][0])
            runs = self.runs
            if "status" in query:
                runs = [run for run in runs if run["conclusion"] == query["status"][0]][:1000]
            return {"total_count": len(runs), "workflow_runs": runs[(page - 1) * size:page * size]}
        if path == "actions/workflows/synology-release.yml":
            return {"id": 99}
        if path.startswith("compare/"):
            return {"status": self.comparison}
        run_id = int(path.split("/")[2])
        if "/" not in path.removeprefix("actions/runs/"):
            return next(run for run in self.runs if run["id"] == run_id)
        return {"total_count": 1, "artifacts": [self.artifacts[run_id]]}

    def download(self, artifact_id, size, destination):
        self.downloads.append(artifact_id)
        if self.reject_download == "offline":
            raise cli.ReleaseError("artifact download failed")
        shutil.copyfile(self.root / f"{artifact_id}.zip", destination)
        if self.reject_download == "corrupt":
            with destination.open("ab") as file:
                file.write(b"corrupt")

    def publish(self, number, target, source, rollback, *, actor=123, attempt=1):
        image = image_ref(target, source, number)
        image_id = "sha256:" + source[0] * 64
        manifest = {"schema": 1, "repository": REPO, "run_id": number, "workflow_sha": CONTROLLER,
                    "target": target, "source_sha": source, "rollback_from": rollback,
                    "platform": "linux/amd64", "image": image, "image_id": image_id}
        archive = self.root / "synthetic-image.tar"
        archive.write_text(f"{image} {image_id} {source}")
        manifest["image_sha256"] = digest(archive)
        destination = self.root / f"{number}.zip"
        with ZipFile(destination, "w") as artifact:
            artifact.write(archive, "image.tar")
            artifact.writestr("manifest.json", json.dumps(manifest))
            for name in COMPOSE_FILES[target]:
                artifact.write(SCRIPTS.parent.parent / name, name)
        self.register(number, manifest, destination, actor, attempt)
        return image

    def register(self, number, manifest, destination, actor=123, attempt=1):
        target, source, rollback, controller = (manifest[key] for key in
                                                 ("target", "source_sha", "rollback_from", "workflow_sha"))
        repo = {"id": 42, "full_name": manifest["repository"]}
        self.runs.insert(0, {
            "id": number, "workflow_id": 99, "run_number": number, "run_attempt": attempt, "event": "workflow_dispatch",
            "display_title": f"Synology {target} {source} from {rollback}", "actor": {"id": actor},
            "head_branch": "main", "status": "completed", "conclusion": "success",
            "head_sha": controller, "path": ".github/workflows/synology-release.yml@main",
            "repository": repo, "head_repository": repo,
        })
        self.artifacts[number] = {
            "id": number, "name": f"synology-{target}", "expired": False,
            "size_in_bytes": destination.stat().st_size, "digest": f"sha256:{digest(destination)}",
            "workflow_run": {"id": number, "head_branch": "main", "head_sha": controller,
                             "repository_id": 42, "head_repository_id": 42},
        }


@pytest.fixture
def host(tmp_path, monkeypatch):
    # Hosted CI runs as an ordinary user. All files and Docker operations here are synthetic.
    uid = os.geteuid()
    monkeypatch.setattr(cli.os, "geteuid", lambda: 0)
    private = cli.private
    monkeypatch.setattr(cli, "private", lambda path, directory=False:
                        path if uid != 0 and path.stat().st_uid == uid else private(path, directory))
    docker = tmp_path / "docker"
    docker.write_text(f"#!{sys.executable}\n" + (Path(__file__).parent / "fake_docker.py").read_text())
    docker.chmod(0o700)
    # The original Core scripts keep their root check. Supply id only for this
    # unprivileged test process, while retaining the actual timeout/Compose calls.
    fake_id = tmp_path / "id"
    fake_id.write_text("#!/bin/sh\nprintf '0\\n'\n")
    fake_id.chmod(0o700)
    init = cli.Deployment.__init__

    def initialize(self, *args):
        init(self, *args)
        self.env["PATH"] = f"{tmp_path}:{self.env['PATH']}"

    monkeypatch.setattr(cli.Deployment, "__init__", initialize)
    return Actions(tmp_path, monkeypatch)


def config_at(root, target):
    config = root / "config.json"
    token = root / "token"
    token.write_text("fixture")
    token.chmod(0o600)
    control = root / "control"
    control.mkdir(mode=0o700)
    env = root / "service.env"
    env.write_text("MARKET_EDITION_INSTANCE=fixture\n")
    config.write_text(json.dumps({
        "target": target, "control_dir": str(control), "project": "fixture",
        "repository": REPO, "operator_ids": [123], "token_file": str(token),
        "docker": str(root / "docker"), "env_file": str(env), "core_root": str(root),
        "manual_repo": str(SCRIPTS.parent.parent),
    }))
    config.chmod(0o600)
    return config


def invoke(config, command, monkeypatch, capsys, error=None, *, run_id=None):
    args = [str(SCRIPTS / "deploy-release.py"), str(config), command]
    if run_id is not None:
        args.extend(["--run-id", str(run_id)])
    monkeypatch.setattr(sys, "argv", args)
    if not error:
        cli.main()
        return capsys.readouterr().out
    with pytest.raises(SystemExit) as exit_info:
        cli.main()
    assert exit_info.value.code == 1
    assert error in capsys.readouterr().err


def calls(root):
    if not (root / "docker.log").exists():
        return []
    return [json.loads(line) for line in (root / "docker.log").read_text().splitlines()]


def test_receiver_cli_processes_queued_releases_in_compatible_order(host, monkeypatch, capsys):
    config = config_at(host.root, "market-edition")
    host.publish(1, "market-edition", BASE, "none")
    invoke(config, "poll", monkeypatch, capsys)
    host.publish(2, "market-edition", NEXT, BASE)
    host.publish(3, "market-edition", FINAL, NEXT)
    host.publish(4, "core", "5" * 40, BASE)
    host.publish(5, "market-edition", "6" * 40, BASE)
    host.runs[0]["head_branch"] = "feature"
    host.publish(6, "market-edition", "7" * 40, "8" * 40)
    # The preceding release sits on an older page than its dependent release.
    other_target = next(run for run in host.runs if run["run_number"] == 4)
    host.runs[:0] = [dict(other_target, id=n, run_number=n) for n in range(106, 10, -1)]

    assert f"Verified market-edition {NEXT}" in invoke(config, "poll", monkeypatch, capsys)
    assert host.downloads == [1, 2]
    assert f"Verified market-edition {FINAL}" in invoke(config, "poll", monkeypatch, capsys)
    assert host.downloads == [1, 2, 3]
    status = json.loads(invoke(config, "status", monkeypatch, capsys))
    assert (status["current"], status["previous"]) == (FINAL, NEXT)
    before = calls(host.root)
    invoke(config, "poll", monkeypatch, capsys, f"rollback_from={FINAL}")
    assert host.downloads == [1, 2, 3] and calls(host.root) == before


def test_poll_crosses_filtered_limit_and_preserves_in_progress_predecessor(host, monkeypatch, capsys):
    config = config_at(host.root, "market-edition")
    host.publish(1, "market-edition", BASE, "none")
    invoke(config, "poll", monkeypatch, capsys)
    host.publish(2, "market-edition", NEXT, BASE)
    predecessor = host.runs[0]
    predecessor.update(status="in_progress", conclusion=None)
    host.publish(3, "market-edition", FINAL, NEXT)
    dependent = host.runs[0]
    host.publish(4, "core", "5" * 40, BASE)
    other_target = host.runs[0]
    host.runs.insert(0, dict(dependent, id=5, run_number=5, conclusion="failure"))
    host.runs[:0] = [dict(other_target, id=n, run_number=n) for n in range(1005, 5, -1)]

    before = calls(host.root)
    invoke(config, "poll", monkeypatch, capsys, f"rollback_from={BASE}")
    assert calls(host.root) == before and host.downloads == [1]
    assert any(parse_qs(urlsplit(path).query).get("page") == ["11"] for path in host.reads)
    state_path = host.root / "control" / "deployment.json"
    state = json.loads(state_path.read_text())
    assert state["handled"] == 1 and state["handled_runs"] == list(range(4, 1006))

    predecessor.update(status="completed", conclusion="success")
    assert f"Verified market-edition {NEXT}" in invoke(config, "poll", monkeypatch, capsys)
    assert f"Verified market-edition {FINAL}" in invoke(config, "poll", monkeypatch, capsys)
    state = json.loads(state_path.read_text())
    assert state["handled"] == 1005 and not state["handled_runs"]
    host.reads.clear()
    assert "No new approved" in invoke(config, "poll", monkeypatch, capsys)
    assert len(host.reads) == 1 and host.downloads == [1, 2, 3]


def test_direct_poll_recovers_when_listing_is_incomplete(host, monkeypatch, capsys):
    config = config_at(host.root, "market-edition")
    host.publish(1, "market-edition", BASE, "none")
    invoke(config, "poll", monkeypatch, capsys)
    host.publish(2, "market-edition", NEXT, BASE)
    host.publish(3, "market-edition", FINAL, NEXT)
    host.publish(4, "core", "5" * 40, BASE)
    read = host.read
    monkeypatch.setattr(host, "read", lambda path: {"total_count": 4, "workflow_runs": []}
                        if path.startswith("actions/workflows/") and "/runs?" in path else read(path))
    before = calls(host.root)
    invoke(config, "poll", monkeypatch, capsys, "--run-id")
    invoke(config, "poll", monkeypatch, capsys, "not an approved", run_id=4)
    invoke(config, "poll", monkeypatch, capsys, f"rollback_from={BASE}", run_id=3)
    assert calls(host.root) == before and host.downloads == [1]
    assert f"Verified market-edition {NEXT}" in invoke(config, "poll", monkeypatch, capsys, run_id=2)
    assert f"Verified market-edition {FINAL}" in invoke(config, "poll", monkeypatch, capsys, run_id=3)
    assert "No new approved" in invoke(config, "poll", monkeypatch, capsys, run_id=3)
    assert host.downloads == [1, 2, 3]


@pytest.mark.parametrize("predecessor_run", [2, 4], ids=["earlier-run-completes", "new-run-fills-gap"])
def test_receiver_cli_reports_gap_without_consuming_runs(host, monkeypatch, capsys, predecessor_run):
    config = config_at(host.root, "market-edition")
    host.publish(1, "market-edition", BASE, "none")
    invoke(config, "poll", monkeypatch, capsys)
    host.publish(3, "market-edition", FINAL, NEXT)
    state = host.root / "control" / "deployment.json"
    before = state.read_bytes(), calls(host.root)
    for _ in range(2):
        invoke(config, "poll", monkeypatch, capsys, f"rollback_from={BASE}")
    assert before == (state.read_bytes(), calls(host.root))
    assert host.downloads == [1]

    host.publish(predecessor_run, "market-edition", NEXT, BASE)
    host.runs.sort(key=lambda run: run["run_number"], reverse=True)
    assert f"Verified market-edition {NEXT}" in invoke(config, "poll", monkeypatch, capsys)
    assert f"Verified market-edition {FINAL}" in invoke(config, "poll", monkeypatch, capsys)
    assert f"Verified market-edition {NEXT}" in invoke(config, "rollback", monkeypatch, capsys)
    assert "No new approved" in invoke(config, "poll", monkeypatch, capsys)
    assert host.downloads == [1, predecessor_run, 3]


@pytest.mark.parametrize("expired", [False, True], ids=["valid", "expired"])
def test_reissued_predecessor_preserves_earlier_dependent_run(host, monkeypatch, capsys, expired):
    config = config_at(host.root, "market-edition")
    host.publish(1, "market-edition", BASE, "none")
    invoke(config, "poll", monkeypatch, capsys)
    host.publish(2, "market-edition", NEXT, BASE)
    host.publish(3, "market-edition", FINAL, NEXT)
    if expired:
        host.artifacts[2]["expired"] = True
        invoke(config, "poll", monkeypatch, capsys, "artifact is expired")
    host.publish(4, "market-edition", NEXT, BASE)
    assert f"Verified market-edition {NEXT}" in invoke(config, "poll", monkeypatch, capsys)
    assert f"Verified market-edition {FINAL}" in invoke(config, "poll", monkeypatch, capsys)
    assert f"Verified market-edition {NEXT}" in invoke(config, "rollback", monkeypatch, capsys)
    assert "No new approved" in invoke(config, "poll", monkeypatch, capsys)
    assert host.downloads == [1, 4, 3]


def test_legacy_handled_boundary_excludes_replay_but_preserves_new_gaps(host, monkeypatch, capsys):
    config = config_at(host.root, "market-edition")
    host.publish(1, "market-edition", BASE, "none")
    invoke(config, "poll", monkeypatch, capsys)
    state_path = host.root / "control" / "deployment.json"
    state = json.loads(state_path.read_text())
    state["handled"] = 5
    state.pop("handled_runs", None)
    state_path.write_text(json.dumps(state))

    host.publish(4, "market-edition", "5" * 40, BASE)
    host.publish(6, "market-edition", FINAL, NEXT)
    host.publish(7, "market-edition", NEXT, BASE)
    assert f"Verified market-edition {NEXT}" in invoke(config, "poll", monkeypatch, capsys)
    assert f"Verified market-edition {FINAL}" in invoke(config, "poll", monkeypatch, capsys)
    assert "No new approved" in invoke(config, "poll", monkeypatch, capsys)
    assert host.downloads == [1, 7, 6]


def test_already_current_dispatch_does_not_restart_or_consume_a_saved_retry(host, monkeypatch, capsys):
    config = config_at(host.root, "market-edition")
    host.publish(1, "market-edition", BASE, "none")
    invoke(config, "poll", monkeypatch, capsys)
    host.publish(2, "market-edition", BASE, "none")
    assert f"Already verified market-edition {BASE}" in invoke(config, "poll", monkeypatch, capsys)
    before = calls(host.root)
    assert "No new approved" in invoke(config, "poll", monkeypatch, capsys)
    assert calls(host.root) == before and host.downloads == [1]

    failure = host.root / "fail-cutover"
    failure.write_text(host.publish(3, "market-edition", NEXT, BASE))
    invoke(config, "poll", monkeypatch, capsys, "recovery completed")
    host.publish(4, "market-edition", BASE, "none")
    before = calls(host.root)
    assert "use retry" in invoke(config, "poll", monkeypatch, capsys)
    assert calls(host.root) == before and host.downloads == [1, 3]
    assert json.loads(invoke(config, "status", monkeypatch, capsys))["retry"] == NEXT
    failure.unlink()
    assert f"Verified market-edition {NEXT}" in invoke(config, "retry", monkeypatch, capsys)


@pytest.mark.parametrize("legacy", [False, True], ids=["current-state", "legacy-state"])
def test_receiver_cli_success_failed_upgrade_restore_and_retry(host, monkeypatch, capsys, legacy):
    root = host.root
    config = config_at(root, "market-edition")
    host.publish(1, "market-edition", BASE, "none")
    assert f"Verified market-edition {BASE}" in invoke(config, "poll", monkeypatch, capsys)
    assert "No new approved" in invoke(config, "poll", monkeypatch, capsys)
    failure = root / "fail-cutover"
    original = host.publish(2, "market-edition", NEXT, BASE)
    host.publish(3, "market-edition", FINAL, NEXT)
    failed_run = 2 if legacy else 4
    failure.write_text(original if legacy else host.publish(failed_run, "market-edition", NEXT, BASE))
    data = root / "synthetic-state"
    data.write_bytes(b"existing receiver data")
    invoke(config, "poll", monkeypatch, capsys, "cutover failed and recovery completed")
    status = json.loads(invoke(config, "status", monkeypatch, capsys))
    assert (status["current"], status["retry"], status["pending"]) == (BASE, NEXT, None)
    if legacy:
        state_path = root / "control" / "deployment.json"
        state = json.loads(state_path.read_text())
        # The previous implementation recorded the failed run only via the high-water mark.
        state.pop("handled_runs", None)
        for record in (state["current"], state["retry"]):
            record.pop("superseded_runs", None)
        assert state["handled"] == 2
        state_path.write_text(json.dumps(state))
    before = calls(root)
    invoke(config, "poll", monkeypatch, capsys, "use retry")
    assert calls(root) == before and host.downloads == [1, failed_run]
    failure.unlink()
    assert f"Verified market-edition {NEXT}" in invoke(config, "retry", monkeypatch, capsys)
    assert data.read_bytes() == b"existing receiver data"
    assert sum(command[0] == "load" for command in calls(root)) == 2
    assert f"Verified market-edition {FINAL}" in invoke(config, "poll", monkeypatch, capsys)
    assert f"Verified market-edition {NEXT}" in invoke(config, "rollback", monkeypatch, capsys)
    assert "No new approved" in invoke(config, "poll", monkeypatch, capsys)
    assert host.downloads == [1, failed_run, 3]


def test_first_receiver_failure_stops_then_retries(host, monkeypatch, capsys):
    config = config_at(host.root, "market-edition")
    failure = host.root / "fail-cutover"
    failure.write_text(host.publish(1, "market-edition", BASE, "none"))
    invoke(config, "poll", monkeypatch, capsys, "recovery completed")
    assert any(command[-2:] == ["stop", "market-edition"] for command in calls(host.root))
    failure.unlink()
    assert f"Verified market-edition {BASE}" in invoke(config, "retry", monkeypatch, capsys)


def test_interrupted_cutover_recovers_before_processing_new_runs(host, monkeypatch, capsys):
    config = config_at(host.root, "market-edition")
    host.publish(1, "market-edition", BASE, "none")
    invoke(config, "poll", monkeypatch, capsys)
    host.publish(2, "market-edition", NEXT, BASE)
    activate = cli.Deployment.activate

    def interrupted(self, candidate):
        activate(self, candidate)
        raise KeyboardInterrupt

    with monkeypatch.context() as fault:
        fault.setattr(cli.Deployment, "activate", interrupted)
        with pytest.raises(KeyboardInterrupt):
            invoke(config, "poll", fault, capsys)
    status = json.loads(invoke(config, "status", monkeypatch, capsys))
    assert (status["current"], status["pending"]) == (BASE, NEXT)
    host.publish(3, "market-edition", FINAL, NEXT)
    invoke(config, "poll", monkeypatch, capsys, "interrupted cutover recovered")
    status = json.loads(invoke(config, "status", monkeypatch, capsys))
    assert (status["current"], status["retry"], status["pending"]) == (BASE, NEXT, None)
    invoke(config, "poll", monkeypatch, capsys, "use retry")
    assert host.downloads == [1, 2]


def test_download_trust_checks_and_lock_precede_docker(host, monkeypatch, capsys):
    config = config_at(host.root, "market-edition")
    host.publish(1, "market-edition", BASE, "none")
    host.publish(2, "market-edition", NEXT, "none", actor=999)
    host.publish(3, "market-edition", NEXT, "none", attempt=2)
    host.comparison = "diverged"
    invoke(config, "poll", monkeypatch, capsys, "outside protected main")
    host.comparison = "ahead"
    releases, retired = GitHub(REPO, [123], "fixture").releases_after("market-edition", 0, set())
    (run, source, rollback), = releases
    assert (run["id"], source, rollback) == (1, BASE, "none")
    assert retired == {2, 3}
    read = host.read
    with monkeypatch.context() as bad_source:
        bad_source.setattr(host, "read", lambda path: {"status": "diverged"}
                           if path.startswith(f"compare/{BASE}...{CONTROLLER}?") else read(path))
        invoke(config, "poll", monkeypatch, capsys, "source commit is outside the approved workflow history")
    host.reject_download = "offline"
    invoke(config, "poll", monkeypatch, capsys, "artifact download failed")
    host.reject_download = "corrupt"
    invoke(config, "poll", monkeypatch, capsys, "artifact SHA-256 mismatch")
    with cli.locked(host.root / "control"):
        invoke(config, "poll", monkeypatch, capsys, "another deployment")
    assert calls(host.root) == []


def test_core_uses_existing_preflight_and_cutover_shells(host, monkeypatch, capsys):
    root = host.root
    config = config_at(root, "core")
    (root / "service.env").write_text(
        "INVESTKITCHEN_PORTFOLIO_AUTHORITY=checkpoint\n"
        "INVESTKITCHEN_MARKET_PROVIDER=toss-native-subprocess\n"
    )
    for name in ("runtime-data/personal/personal-data-manifest.json",
                 "runtime-data/state/portfolio-checkpoints/checkpoints.jsonl",
                 "secrets/trademind-runtime-api-key", "secrets/toss-market-readonly.json"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("fixture")
    baseline = image_ref("core", BASE, 1)
    (root / "docker.json").write_text(json.dumps({
        "images": {baseline: "sha256:" + "1" * 64 + f" linux/amd64 {BASE}"},
        "container": {"image": baseline, "status": "healthy"},
    }))
    assert f"Adopted verified core {BASE}" in invoke(config, "adopt", monkeypatch, capsys)
    host.publish(2, "core", NEXT, BASE)
    failure = root / "fail-core-preflight"
    failure.touch()
    invoke(config, "poll", monkeypatch, capsys, "Core install-image.sh failed")
    assert not any("up" in command for command in calls(root))
    failure.unlink()
    assert f"Verified core {NEXT}" in invoke(config, "poll", monkeypatch, capsys)
    trace = calls(root)
    preflight = next(command for command in trace if "preflight" in command)
    assert "--portfolio-checkpoint-root" in preflight and "--market-secret-file" in preflight
    assert any("--version" in command for command in trace)
    assert any(command[-6:] == ["up", "-d", "--no-build", "--pull", "never", "runtime"]
               for command in trace)
    assert sum(command[0] == "load" for command in trace) == 1
