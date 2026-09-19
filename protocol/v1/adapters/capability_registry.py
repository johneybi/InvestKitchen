from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def load_instance_manifest(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("instance manifest must be an object")
    return value


def get_capabilities(manifest: dict[str, Any]) -> dict[str, Any]:
    """Project installed/readiness/client-exposure state from an instance manifest."""
    kernel = set(str(v) for v in manifest.get("kernel_capabilities", []))
    extensions = {
        str(extension["extension_id"]): extension
        for extension in manifest.get("extensions", [])
        if isinstance(extension, dict) and extension.get("extension_id")
    }
    bindings = {
        str(capability): str(extension_id)
        for capability, extension_id in (manifest.get("capability_bindings") or {}).items()
    }
    exposure = manifest.get("client_exposure") or {}

    provided: dict[str, dict[str, Any]] = {}
    for extension_id, extension in extensions.items():
        for capability in extension.get("provides", []):
            if not isinstance(capability, dict) or not capability.get("capability_id"):
                continue
            provided[str(capability["capability_id"])] = {
                "extension_id": extension_id,
                "contract_version": capability.get("contract_version"),
                "requires": extension.get("requires", []),
                "extension_type": extension.get("extension_type"),
            }

    names = sorted(kernel | set(provided) | set(exposure) | set(bindings))
    rows: list[dict[str, Any]] = []
    for capability in names:
        if capability in kernel:
            installed = True
            ready = True
            provider = "kernel"
            reason = None
        elif capability in provided:
            provider_info = provided[capability]
            provider = provider_info["extension_id"]
            bound = bindings.get(capability) == provider
            missing_required: list[str] = []
            for requirement in provider_info.get("requires", []):
                if not isinstance(requirement, dict) or requirement.get("optional"):
                    continue
                required_capability = str(requirement.get("capability_id") or "")
                if not required_capability:
                    continue
                if required_capability not in kernel and required_capability not in provided:
                    missing_required.append(required_capability)
            installed = True
            ready = bound and not missing_required
            reason = None
            if not bound:
                reason = "capability_not_bound"
            elif missing_required:
                reason = "missing_required_dependencies:" + ",".join(sorted(missing_required))
        else:
            installed = False
            ready = False
            provider = None
            reason = "not_installed"

        client_state = str(exposure.get(capability) or "disabled")
        client_usable = ready and client_state == "supported"
        if client_state == "supported" and not ready:
            reason = reason or "client_exposed_but_not_ready"
        rows.append(
            {
                "capability": capability,
                "installed": installed,
                "ready": ready,
                "provider": provider,
                "client_exposure": client_state,
                "client_usable": client_usable,
                "reason": reason,
            }
        )
    return {
        "instance_id": manifest.get("instance_id"),
        "profile": manifest.get("profile"),
        "capabilities": rows,
    }
