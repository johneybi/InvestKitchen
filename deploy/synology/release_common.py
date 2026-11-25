"""Shared Synology release artifact format. Uses only the Python standard library."""

from hashlib import file_digest
import re

SHA = re.compile(r"[0-9a-f]{40}")
SHA256 = re.compile(r"[0-9a-f]{64}")
IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
COMPOSE_FILES = {
    "core": tuple("deploy/synology/" + name for name in (
        "compose.yml", "control-plane-api-key.example", "toss-market-readonly.example.json",
        "account-bindings.example.json", "toss-account-readonly.example.json",
        "nhplug-account-readonly.example.json",
    )),
    "market-edition": ("deploy/synology/market-edition.compose.yml",),
}


class ReleaseError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise ReleaseError(message)


def digest(path):
    with path.open("rb") as stream:
        return file_digest(stream, "sha256").hexdigest()


def image_ref(target, source, run_id):
    if target == "core":
        return f"trademind-runtime:synology-{source}-{run_id}"
    return f"investkitchen-market-edition:{source}-{run_id}"
