"""Select and package a GitHub-approved source commit without using a worktree."""

import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile

from release_common import COMPOSE_FILES, SHA, SHA256, ReleaseError, digest, image_ref, require

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent


def git(*args):
    return subprocess.check_output(["git", "-C", str(ROOT), *args], text=True).strip()


def selection():
    target = os.environ["TARGET"]
    source = os.environ["SOURCE_SHA"]
    rollback = os.environ["ROLLBACK_FROM"]
    require(os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch"
            and os.environ.get("GITHUB_REF") == "refs/heads/main"
            and os.environ.get("GITHUB_REF_PROTECTED") == "true"
            and os.environ.get("GITHUB_RUN_ATTEMPT") == "1",
            "dispatch this workflow on protected main as a new run")
    allowed = os.environ.get("RELEASE_ACTOR_IDS", "").split()
    require(all(actor.isdecimal() for actor in allowed)
            and os.environ.get("RELEASE_ACTOR_ID") in allowed, "release operator is unauthorized")
    require(target in COMPOSE_FILES and SHA.fullmatch(source), "invalid target or source SHA")
    require(SHA.fullmatch(rollback) or (target == "market-edition" and rollback == "none"),
            "provide the current state-compatible SHA, or none for first receiver install")
    controller = os.environ["GITHUB_SHA"]
    require(git("rev-parse", "HEAD") == controller and git("cat-file", "-t", source) == "commit",
            "expected the workflow checkout and an exact commit object")
    require(subprocess.run(["git", "-C", str(ROOT), "merge-base", "--is-ancestor", source, controller],
                           check=False).returncode == 0, "selected commit is outside protected main history")
    return source


def core_archive_identity(archive, loaded_id, source):
    """Use the config digest reported by the supported DSM Docker engine."""
    with tarfile.open(archive) as bundle:
        rows = json.load(bundle.extractfile("manifest.json"))
        index = json.load(bundle.extractfile("index.json"))
        require(len(rows) == 1 and len(index["manifests"]) == 1, "expected a single Core image archive")
        data = bundle.extractfile(rows[0]["Config"]).read()
        config_id = "sha256:" + sha256(data).hexdigest()
        config = json.loads(data)
        require(config["config"]["Labels"]["org.opencontainers.image.revision"] == source
                and config["os"] == "linux" and config["architecture"] == "amd64",
                "Core archive config has unexpected platform or revision")
        manifest_id = index["manifests"][0]["digest"]
        require(manifest_id.startswith("sha256:") and SHA256.fullmatch(manifest_id[7:]),
                "invalid Core image manifest digest")
        require(loaded_id in (config_id, manifest_id), "builder loaded a different Core image")
        return config_id


def package(out):
    target = os.environ["TARGET"]
    source = selection()
    run_id = int(os.environ["GITHUB_RUN_ID"])
    image = image_ref(target, source, run_id)
    archive = out / "image.tar"
    out.mkdir(parents=True)
    dockerfile = "Dockerfile" if target == "core" else "market-edition.Dockerfile"
    files = [".dockerignore", "protocol", f"deploy/synology/{dockerfile}", *COMPOSE_FILES[target]]
    if target == "core":
        files.append("deploy/synology/container-entrypoint.sh")
    with tempfile.TemporaryDirectory() as directory:
        context = Path(directory)
        snapshot = context / "source.tar"
        subprocess.run(["git", "-C", str(ROOT), "archive", "--format=tar", "-o", str(snapshot),
                        source, *files], check=True)
        subprocess.run(["tar", "-xf", str(snapshot), "-C", str(context)], check=True)
        snapshot.unlink()
        if target == "core":
            subprocess.run(["sh", str(HERE / "build-image.sh")], check=True, env={
                **os.environ, "TRADEMIND_SOURCE_COMMIT": source, "TRADEMIND_RUNTIME_IMAGE_REF": image,
                "TRADEMIND_IMAGE_OUTPUT_DIR": str(out), "TRADEMIND_IMAGE_ARCHIVE": str(archive),
                "TRADEMIND_BUILD_CONTEXT": str(context),
            })
            archive.with_suffix(".tar.sha256").unlink()
        else:
            subprocess.run([
                "docker", "buildx", "build", "--platform", "linux/amd64", "--provenance=false",
                "--sbom=false", "--build-arg", f"INVESTKITCHEN_SOURCE_COMMIT={source}", "-t", image,
                "-f", str(context / "deploy/synology" / dockerfile),
                "--output", f"type=docker,dest={archive},oci-mediatypes=false", str(context),
            ], check=True)
            subprocess.run(["docker", "load", "-i", str(archive)], check=True)
            subprocess.run(["docker", "run", "--rm", "--network", "none", image, "--help"],
                           stdout=subprocess.DEVNULL, check=True)
        for name in COMPOSE_FILES[target]:
            destination = out / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes((context / name).read_bytes())

    image_id, platform, revision = subprocess.check_output([
        "docker", "image", "inspect", "--format",
        '{{.Id}} {{.Os}}/{{.Architecture}} {{index .Config.Labels "org.opencontainers.image.revision"}}',
        image,
    ], text=True).split()
    require(platform == "linux/amd64" and revision == source, "unexpected loaded image identity")
    if target == "core":
        image_id = core_archive_identity(archive, image_id, source)
    (out / "manifest.json").write_text(json.dumps({
        "schema": 1, "repository": os.environ["GITHUB_REPOSITORY"].lower(), "run_id": run_id,
        "workflow_sha": os.environ["GITHUB_SHA"], "target": target, "source_sha": source,
        "rollback_from": os.environ["ROLLBACK_FROM"], "platform": platform,
        "image": image, "image_id": image_id, "image_sha256": digest(archive),
    }, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("select")
    sub.add_parser("package").add_argument("out", type=Path)
    args = parser.parse_args()
    try:
        if args.command == "select":
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                print(f"source_sha={selection()}", file=output)
        else:
            package(args.out)
    except ReleaseError as error:
        parser.exit(1, f"{error}\n")
