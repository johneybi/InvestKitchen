"""Pull and activate an approved InvestKitchen release on a Synology host."""

import argparse
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import time
from zipfile import BadZipFile

from release_common import COMPOSE_FILES, IMAGE_ID, SHA, ReleaseError, digest, require
from release_github import GitHub

HERE = Path(__file__).resolve().parent
INSPECT_IMAGE = '{{.Id}} {{.Os}}/{{.Architecture}} {{index .Config.Labels "org.opencontainers.image.revision"}}'
INSPECT_CONTAINER = '{{.Image}} {{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}'
MARKET_PROBE = r"""
from datetime import datetime, timezone
from pathlib import Path
import socket
import ssl

from cryptography.x509 import load_pem_x509_certificate

# The listener starts after DB initialization. Require its pinned leaf and a
# TLS 1.3 certificate-required alert without loading a publisher credential.
pem = Path("/trust/nas.pem").read_text().split("-----END CERTIFICATE-----")[0]
pem = pem.strip() + "\n-----END CERTIFICATE-----"
leaf = ssl.PEM_cert_to_DER_cert(pem)
cert = load_pem_x509_certificate(pem.encode())
if not cert.not_valid_before_utc <= datetime.now(timezone.utc) < cert.not_valid_after_utc:
    raise SystemExit(1)
tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
tls.check_hostname = False
tls.verify_mode = ssl.CERT_NONE
tls.minimum_version = ssl.TLSVersion.TLSv1_3
with socket.create_connection(("127.0.0.1", 8443), timeout=3) as connection:
    with tls.wrap_socket(connection) as channel:
        if channel.getpeercert(binary_form=True) != leaf:
            raise SystemExit(1)
        try:
            channel.recv(1)
        except ssl.SSLError as error:
            if error.reason == "TLSV13_ALERT_CERTIFICATE_REQUIRED":
                raise SystemExit(0)
raise SystemExit(1)
"""


def private(path, directory=False):
    require(path.is_absolute(), "control and configuration paths must be absolute")
    info = path.lstat()
    require(info.st_uid == 0 and not info.st_mode & 0o077
            and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)),
            "release configuration, control directory and token must be root-owned and private")
    return path


def write_state(path, state):
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent, delete=False) as file:
        json.dump(state, file)
        file.flush()
        os.fsync(file.fileno())
    os.replace(file.name, path)
    sync_directory(path.parent)


def sync_directory(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def locked(root):
    with (root / "deploy.lock").open("ab") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ReleaseError("another deployment is running") from error
        yield lock.fileno()


class Deployment:
    def __init__(self, config, root, lock_fd):
        self.config = config
        self.root = root
        self.lock_fd = lock_fd
        self.target = config["target"]
        require(self.target in COMPOSE_FILES, "unknown deployment target")
        self.core = self.target == "core"
        self.service = "runtime" if self.core else "market-edition"
        self.key = "TRADEMIND_RUNTIME_IMAGE_REF" if self.core else "MARKET_EDITION_IMAGE"
        self.project = config["project"]
        require(re.fullmatch(r"[a-z0-9][a-z0-9_-]*", self.project), "invalid Compose project name")
        self.docker = config["docker"]
        require(Path(self.docker).is_absolute() and os.access(self.docker, os.X_OK),
                "configure an absolute executable Docker CLI path")
        self.state_file = root / "deployment.json"
        self.state = (json.loads(self.state_file.read_text(encoding="utf-8")) if self.state_file.exists()
                      else {"handled": 0, "current": None, "previous": None, "pending": None, "retry": None})
        scope = [self.target, self.project, config["repository"].lower()]
        require(self.state.get("scope", scope) == scope, "deployment directory belongs to another service")
        self.state["scope"] = scope
        self.env = {"PATH": os.defpath + ":/usr/local/bin:/usr/syno/bin",
                    "HOME": str(Path.home()), "LANG": "C", "COMPOSE_PROJECT_NAME": self.project}

    def save(self):
        write_state(self.state_file, self.state)

    def mark_handled(self, *numbers):
        handled = self.state["handled"]
        handled_runs = set(self.state.get("handled_runs", ()))
        handled_runs.update(number for number in numbers if number > handled)
        # The legacy watermark stays a replay floor. Only a contiguous set of
        # handled runs can advance it without discarding an unhandled run.
        while handled + 1 in handled_runs:
            handled += 1
            handled_runs.remove(handled)
        self.state.update(handled=handled, handled_runs=sorted(handled_runs))

    def run(self, phase, args, *, env=None, timeout=120, capture=False, check=True):
        try:
            result = subprocess.run(args, env=self.env if env is None else env,
                                    stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, text=True, timeout=timeout,
                                    pass_fds=(self.lock_fd,))
        except (OSError, subprocess.TimeoutExpired) as error:
            raise ReleaseError(f"{phase} failed or timed out") from error
        require(not check or result.returncode == 0, f"{phase} failed (exit {result.returncode})")
        return result.stdout.strip() if capture else result.returncode

    def directory(self, record):
        return self.root / record["directory"]

    def compose(self, directory, *args, capture=False):
        return self.run("Compose " + args[0], [
            self.docker, "compose", "--project-name", self.project,
            "--env-file", str(directory / "deploy.env"),
            "-f", str(directory / COMPOSE_FILES[self.target][0]), *args,
        ], capture=capture)

    def source_env(self, image=None, path=None):
        text = (Path(self.config["env_file"]) if path is None else path).read_text(encoding="utf-8")
        assignment = re.compile(rf"^[ \t]*(?:export[ \t]+)?{self.key}[ \t]*=.*$", re.MULTILINE)
        base = assignment.sub("", text).strip()
        if image is None:
            return base
        return f"{base}\n{self.key}={image}\n"

    def image_metadata(self, image):
        result = self.run("image identity", [self.docker, "image", "inspect", "--format",
                                             INSPECT_IMAGE, image], capture=True)
        parts = result.split()
        require(len(parts) == 3 and IMAGE_ID.fullmatch(parts[0])
                and parts[1] == "linux/amd64" and SHA.fullmatch(parts[2]), "unexpected image identity")
        return parts[0], parts[2]

    def ensure_image(self, record):
        expected = record["image_id"], record["source_sha"]
        try:
            loaded = self.image_metadata(record["image"])
        except ReleaseError:
            archive = self.directory(record) / "image.tar"
            require(archive.is_file(), "verified image unavailable locally")
            require(digest(archive) == record["image_sha256"], "retained image archive SHA-256 mismatch")
            self.run("Docker image load", [self.docker, "load", "-i", str(archive)], timeout=300)
            loaded = self.image_metadata(record["image"])
        require(loaded == expected, "loaded image ID or revision differs from the verified release")

    def container(self, directory):
        ids = self.compose(directory, "ps", "--all", "--quiet", self.service, capture=True).split()
        require(len(ids) <= 1, "expected one container in the target Compose project")
        return ids[0] if ids else None

    def readiness(self, record, wait):
        directory = self.directory(record)
        deadline = time.monotonic() + (120 if wait else 0)
        while True:
            container = self.container(directory)
            if container:
                identity = self.run("container inspection", [
                    self.docker, "inspect", "--format", INSPECT_CONTAINER, container,
                ], capture=True).split()
                require(len(identity) == 2 and identity[0] == record["image_id"],
                        "running container does not use the verified image")
                status = identity[1]
                if self.core and status == "healthy":
                    return
                if not self.core and status in ("running", "healthy"):
                    result = self.run("receiver TLS readiness", [
                        self.docker, "exec", container, "python", "-c", MARKET_PROBE,
                    ], timeout=8, check=False)
                    if result == 0:
                        return
                require(status not in ("unhealthy", "restarting", "exited", "dead"), "service failed readiness")
            require(time.monotonic() < deadline, "service did not become ready")
            time.sleep(2)

    def core_script(self, script, record, *args):
        directory = self.directory(record)
        self.run(f"Core {script}", ["sh", str(HERE / script), *args], timeout=420, env={
            **self.env, "INVESTKITCHEN_DOCKER_BIN": self.docker, "INVESTKITCHEN_RELEASE_MODE": "1",
            "INVESTKITCHEN_PREFLIGHT_IMAGE_ID": record["image_id"],
            "TRADEMIND_SYNOLOGY_ROOT": self.config["core_root"],
            "TRADEMIND_RUNTIME_REPO_ROOT": str(directory),
            "TRADEMIND_RUNTIME_ENV_FILE": str(directory / "deploy.env"),
        })

    def activate(self, record):
        self.ensure_image(record)
        if self.core:
            self.core_script("cutover.sh", record)
        else:
            self.compose(self.directory(record), "up", "-d", "--no-build", "--pull", "never", self.service)
        self.readiness(record, wait=not self.core)

    def recover(self):
        pending = self.state["pending"]
        current = self.state["current"]
        candidate = pending["candidate"]
        if current:
            self.activate(current)
            print(f"Restored {self.target} {current['source_sha']}.", flush=True)
        else:
            self.compose(self.directory(candidate), "stop", self.service)
            print(f"Stopped first {self.target} deployment. State was retained.", flush=True)
        if pending["operation"] == "release":
            self.mark_handled(candidate["number"], *candidate.get("superseded_runs", ()))
            self.state["retry"] = candidate
        self.state["pending"] = None
        self.save()

    def switch(self, candidate, operation):
        self.state["pending"] = {"operation": operation, "candidate": candidate}
        self.save()
        try:
            self.activate(candidate)
        except ReleaseError as error:
            print(f"{self.target} cutover failed. {error}", flush=True)
            self.recover()
            raise ReleaseError(f"{self.target} cutover failed and recovery completed") from error
        self.state["previous"], self.state["current"] = self.state["current"], candidate
        if operation == "release":
            self.mark_handled(candidate["number"], *candidate.get("superseded_runs", ()))
        self.state["retry"] = None
        self.state["pending"] = None
        self.save()
        print(f"Verified {self.target} {candidate['source_sha']}.", flush=True)

    def install(self, candidate):
        current = self.state["current"]
        directory = self.directory(candidate)
        if current:
            require(candidate["rollback_from"] == current["source_sha"],
                    "release rollback SHA does not match the verified active version")
            require(self.source_env(path=self.directory(current) / "deploy.env")
                    == self.source_env(path=directory / "deploy.env"),
                    "deployment environment changed since the verified version")
            self.ensure_image(current)
            self.readiness(current, wait=False)
        else:
            require(not self.core and candidate["rollback_from"] == "none", "adopt the live service first")
            container = self.container(directory)
            if container:
                failed = self.state["retry"]
                stopped = self.run("stopped receiver inspection", [
                    self.docker, "inspect", "--format", INSPECT_CONTAINER, container,
                ], capture=True)
                require(failed and stopped in (f"{failed['image_id']} exited", f"{failed['image_id']} created"),
                        "adopt the existing receiver before a release")
        self.ensure_image(candidate)
        self.compose(directory, "config", "--quiet")
        if self.core:
            self.core_script("install-image.sh", candidate, str(directory / "image.tar"))
        self.switch(candidate, "release")

    def adopt(self):
        require(self.state["current"] is None, "a verified deployment is already recorded")
        source = Path(self.config["manual_repo"])
        with tempfile.TemporaryDirectory(dir=self.root) as temp:
            directory = Path(temp)
            for index, name in enumerate(COMPOSE_FILES[self.target]):
                if index and not (source / name).is_file():
                    continue
                path = directory / name
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source / name, path)
            shutil.copyfile(self.config["env_file"], directory / "deploy.env")
            container = self.container(directory)
            require(container, "no running service to adopt")
            image = self.run("running image reference", [
                self.docker, "inspect", "--format", "{{.Config.Image}}", container,
            ], capture=True)
            require(re.fullmatch(r"[a-z0-9][a-z0-9._/-]*:[a-zA-Z0-9_.-]+", image)
                    and not image.startswith("sha256:"),
                    "adoption requires a locally retained tagged image")
            image_id, source_sha = self.image_metadata(image)
            record = {"directory": "baseline", "image": image, "image_id": image_id, "source_sha": source_sha}
            (directory / "deploy.env").write_text(self.source_env(image), encoding="utf-8")
            self.keep_bundle(directory, record)
            self.readiness(record, wait=False)
            self.state["current"] = record
            self.save()
        print(f"Adopted verified {self.target} {source_sha}.")

    def keep_bundle(self, directory, record):
        for path in directory.rglob("*"):
            if path.is_file():
                with path.open("rb") as file:
                    os.fsync(file.fileno())
        for path, _, _ in os.walk(directory, topdown=False):
            sync_directory(path)
        final = self.directory(record)
        # Only an unreferenced staging directory can be replaced on a poll retry.
        require(all(not entry or entry["directory"] != record["directory"]
                    for entry in (self.state["current"], self.state["previous"], self.state["retry"])),
                "release directory already belongs to a recorded deployment")
        if final.exists():
            shutil.rmtree(final)
        directory.rename(final)
        sync_directory(self.root)

    def poll(self, github, run_id=None):
        current = self.state["current"]
        baseline = current["source_sha"] if current else "none"
        handled_runs = set(self.state.get("handled_runs", ()))
        if run_id is None:
            runs, retired = github.releases_after(self.target, self.state["handled"], handled_runs)
            if retired:
                self.mark_handled(*retired)
                self.save()
        else:
            selection = github.release_for_run(self.target, run_id)
            number = selection[0]["run_number"]
            runs = [selection] if number > self.state["handled"] and number not in handled_runs else []
        runs.sort(key=lambda entry: entry[0]["run_number"])
        transitions = [selection for selection in runs if selection[1] != baseline]
        compatible = [selection for selection in transitions if selection[2] == baseline]
        if compatible:
            # Preserve transition order; a fresh dispatch for the same pair can replace its artifact.
            first_source = compatible[0][1]
            *superseded, selection = [entry for entry in compatible if entry[1] == first_source]
        else:
            failed = self.state["retry"]
            hint = f" To reapply {failed['source_sha']}, use retry." if failed else ""
            if transitions:
                require(current or not self.core, "adopt the live service first")
                raise ReleaseError(f"No eligible {self.target} release with rollback_from={baseline}."
                                   f" Wait for or dispatch a compatible predecessor.{hint}")
            if not runs or failed:
                print(f"No new approved {self.target} release.{hint}")
                return
            selection = runs[-1]
            superseded = []

        github.verify_source(selection)
        run, source, _ = selection
        if source == baseline:
            self.readiness(current, wait=False)
            self.mark_handled(run["run_number"])
            self.save()
            print(f"Already verified {self.target} {source}.")
            return
        with tempfile.TemporaryDirectory(dir=self.root) as temp:
            directory = Path(temp)
            candidate = github.fetch(self.target, selection, directory)
            candidate["directory"] = f"run-{run['id']}"
            candidate["superseded_runs"] = [entry[0]["run_number"] for entry in superseded]
            (directory / "deploy.env").write_text(self.source_env(candidate["image"]), encoding="utf-8")
            self.keep_bundle(directory, candidate)
            self.install(candidate)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", type=Path)
    parser.add_argument("command", choices=("poll", "adopt", "retry", "rollback", "status"))
    parser.add_argument("--run-id", type=int, help="poll one approved GitHub run when listing is unavailable")
    args = parser.parse_args()
    os.umask(0o077)
    try:
        require(args.run_id is None or (args.command == "poll" and args.run_id > 0),
                "--run-id requires poll and a positive GitHub run ID")
        require(os.geteuid() == 0, "run the deployment command as root")
        config = json.loads(private(args.config).read_text(encoding="utf-8"))
        root = private(Path(config["control_dir"]), directory=True)
        with locked(root) as lock_fd:
            deployment = Deployment(config, root, lock_fd)
            if args.command == "status":
                status = {key: value["source_sha"] if value else None for key, value in (
                    ("current", deployment.state["current"]), ("previous", deployment.state["previous"]),
                    ("retry", deployment.state["retry"]),
                    ("pending", (deployment.state["pending"] or {}).get("candidate")),
                )}
                status["current_bundle"] = (deployment.state["current"] or {}).get("directory")
                print(json.dumps(status))
                return
            if deployment.state["pending"]:
                deployment.recover()
                raise ReleaseError("interrupted cutover recovered; review status before retrying")
            if args.command == "adopt":
                deployment.adopt()
            elif args.command == "rollback":
                previous = deployment.state["previous"]
                require(previous, "no compatible verified predecessor")
                deployment.switch(previous, "rollback")
            elif args.command == "retry":
                failed = deployment.state["retry"]
                require(failed, "no failed cutover is recorded")
                require(deployment.source_env(path=deployment.directory(failed) / "deploy.env")
                        == deployment.source_env(), "deployment environment changed since failed cutover")
                deployment.install(failed)
            else:
                repository = config["repository"]
                operators = config["operator_ids"]
                require(re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository)
                        and operators and all(type(operator) is int and operator > 0 for operator in operators),
                        "configure the GitHub repository and trusted operator IDs")
                token = private(Path(config["token_file"])).read_text(encoding="utf-8").strip()
                require(token and not any(value.isspace() for value in token), "invalid GitHub read token")
                deployment.poll(GitHub(repository, operators, token), args.run_id)
    except ReleaseError as error:
        parser.exit(1, f"{error}\n")
    except (OSError, ValueError, TypeError, KeyError, BadZipFile):
        parser.exit(1, "release configuration, deployment state, or artifact could not be read\n")


if __name__ == "__main__":
    main()
