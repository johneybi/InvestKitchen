"""Read release provenance and immutable artifacts from the GitHub Actions API."""

import json
from itertools import count
from pathlib import Path
import re
import shutil
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import ZipFile

from release_common import COMPOSE_FILES, IMAGE_ID, SHA, ReleaseError, digest, image_ref, require

WORKFLOW = "synology-release.yml"
BRANCH = "main"
MAX_ARCHIVE = 4 * (1 << 30)


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class GitHub:
    def __init__(self, repository, operators, token):
        self.repository = repository
        self.operators = operators
        self.token = token
        self.api = build_opener(NoRedirect())

    def request(self, path):
        return self.api.open(Request(f"https://api.github.com/repos/{self.repository}/{path}", headers={
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Authorization": f"Bearer {self.token}",
        }), timeout=30)

    def read(self, path):
        try:
            with self.request(path) as response:
                return json.load(response)
        except HTTPError as error:
            if error.code == 429 or (error.code == 403 and (
                    error.headers.get("Retry-After") or error.headers.get("X-RateLimit-Remaining") == "0")):
                raise ReleaseError("GitHub API rate limited; pause polling until the limit resets") from error
            raise ReleaseError(f"GitHub metadata request failed (HTTP {error.code})") from error
        except (URLError, OSError, ValueError) as error:
            raise ReleaseError("GitHub metadata unavailable or malformed") from error

    def workflow_runs_after(self, handled):
        # GitHub applies the 1,000-result search cap to filtered listings. The
        # unfiltered workflow listing also exposes attempts still in progress.
        oldest_seen = None
        size = 100
        for page in count(1):
            result = self.read(f"actions/workflows/{WORKFLOW}/runs?per_page={size}&page={page}")
            runs = result["workflow_runs"]
            numbers = [run["run_number"] for run in runs]
            if numbers:
                oldest = min(numbers)
                require(oldest_seen is None or oldest < oldest_seen,
                        "GitHub run listing did not advance; use poll --run-id for a reviewed run")
                oldest_seen = oldest
                yield from (run for run in runs if run["run_number"] > handled)
                if max(numbers) <= handled:
                    return
            observed = (page - 1) * size + len(runs)
            if observed >= result["total_count"]:
                return
            require(len(runs) == size, "GitHub run listing is incomplete; use poll --run-id for a reviewed run")

    def release_selection(self, target, run):
        rollback = "([0-9a-f]{40}|none)" if target == "market-edition" else "([0-9a-f]{40})"
        match = re.fullmatch(rf"Synology {target} ([0-9a-f]{{40}}) from {rollback}", run["display_title"])
        if (match and run["path"].partition("@")[0] == f".github/workflows/{WORKFLOW}"
                and run["event"] == "workflow_dispatch" and run["run_attempt"] == 1
                and run["head_branch"] == BRANCH
                and run["status"] == "completed" and run["conclusion"] == "success"
                and run["actor"]["id"] in self.operators
                and run["repository"]["full_name"].lower() == self.repository.lower()
                and run["repository"]["id"] == (run.get("head_repository") or {}).get("id")):
            return run, *match.groups()
        return None

    def releases_after(self, target, handled, handled_runs):
        releases = []
        retired = set()
        for run in self.workflow_runs_after(handled):
            number = run["run_number"]
            if number in handled_runs:
                continue
            # An initial attempt may still succeed. A rerun can never restore
            # eligibility, and completed attempts with a conclusion are final.
            if run["run_attempt"] == 1 and (run["status"] != "completed" or not run["conclusion"]):
                continue
            selection = self.release_selection(target, run)
            if selection:
                releases.append(selection)
            else:
                retired.add(number)
        return releases, retired

    def release_for_run(self, target, run_id):
        run = self.read(f"actions/runs/{run_id}")
        workflow = self.read(f"actions/workflows/{WORKFLOW}")
        require(run["id"] == run_id and run["workflow_id"] == workflow["id"],
                "GitHub run is outside the Synology release workflow")
        selection = self.release_selection(target, run)
        require(selection, f"GitHub run {run_id} is not an approved {target} release")
        return selection

    def verify_source(self, selection):
        run, source, _ = selection
        head = run["head_sha"]
        require(SHA.fullmatch(head), "invalid workflow SHA in GitHub metadata")
        # head_branch alone does not prove that the workflow came from refs/heads/main.
        comparison = self.read(f"compare/{head}...{BRANCH}?per_page=1")
        require(comparison["status"] in ("identical", "ahead"),
                "workflow commit is outside protected main history")
        if source != head:
            comparison = self.read(f"compare/{source}...{head}?per_page=1")
            require(comparison["status"] == "ahead", "source commit is outside the approved workflow history")

    def download(self, artifact_id, size, destination):
        try:
            with self.request(f"actions/artifacts/{artifact_id}/zip"):
                raise ReleaseError("expected an artifact download redirect")
        except HTTPError as redirect:
            location = redirect.headers.get("Location", "")
            require(redirect.code == 302 and urlsplit(location).scheme == "https",
                    f"GitHub refused artifact download (HTTP {redirect.code})")
        # The short-lived storage URL receives no GitHub token. Reject further redirects.
        try:
            with self.api.open(location, timeout=60) as response, destination.open("wb") as output:
                remaining = size
                while block := response.read(min(remaining + 1, 1 << 20)):
                    remaining -= len(block)
                    require(remaining >= 0, "artifact download exceeds GitHub metadata size")
                    output.write(block)
            require(remaining == 0, "artifact download was truncated")
        except (URLError, OSError) as error:
            raise ReleaseError("artifact download failed") from error

    def fetch(self, target, selection, destination):
        run, source, rollback_from = selection
        run_id = run["id"]
        result = self.read(f"actions/runs/{run_id}/artifacts?per_page=2")
        require(result["total_count"] == 1, "expected exactly one artifact for the approved run")
        artifact, = result["artifacts"]
        reference = artifact["workflow_run"]
        require(artifact["name"] == f"synology-{target}" and artifact["expired"] is False
                and reference["id"] == run_id and reference["head_sha"] == run["head_sha"]
                and reference["head_branch"] == BRANCH
                and reference["repository_id"] == reference["head_repository_id"] == run["repository"]["id"],
                "artifact is expired or does not match the approved run")
        size = artifact["size_in_bytes"]
        expected = artifact.get("digest", "")
        require(type(size) is int and 0 < size <= MAX_ARCHIVE
                and IMAGE_ID.fullmatch(expected or ""), "artifact metadata is missing a valid size or SHA-256")
        download = destination / "download.zip"
        self.download(artifact["id"], size, download)
        require(f"sha256:{digest(download)}" == expected, "GitHub artifact SHA-256 mismatch")

        files = {"image.tar", "manifest.json", *COMPOSE_FILES[target]}
        with ZipFile(download) as bundle:
            require(len(bundle.namelist()) == len(files) and set(bundle.namelist()) == files,
                    "unexpected files in release archive")
            for name in files:
                info = bundle.getinfo(name)
                require(0 < info.file_size <= (MAX_ARCHIVE if name == "image.tar" else 1 << 20),
                        "release member exceeds size limit")
                output = destination / Path(name)
                output.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as stream, output.open("wb") as file:
                    shutil.copyfileobj(stream, file)
        download.unlink()
        manifest = json.loads((destination / "manifest.json").read_text(encoding="utf-8"))
        expected_fields = {
            "schema": 1, "repository": self.repository.lower(), "run_id": run_id,
            "workflow_sha": run["head_sha"], "target": target, "source_sha": source,
            "rollback_from": rollback_from, "platform": "linux/amd64",
            "image": image_ref(target, source, run_id),
        }
        require(all(manifest.get(name) == value for name, value in expected_fields.items())
                and IMAGE_ID.fullmatch(manifest.get("image_id", ""))
                and digest(destination / "image.tar") == manifest.get("image_sha256"),
                "manifest or image archive does not match the approved release")
        return {**expected_fields, "image_id": manifest["image_id"], "image_sha256": manifest["image_sha256"],
                "number": run["run_number"]}
