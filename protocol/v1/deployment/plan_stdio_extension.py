"""Add the reviewed Plan service to a pinned older stdio runtime.

This entrypoint preserves the installed runtime's other tools and handlers. It
never edits that runtime. All new Plan persistence and approvals use this
release's native service; the older transport keeps its authorization hooks.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import importlib
import importlib.util
import json
import sys
from pathlib import Path


RELEASE_ROOT = Path(__file__).resolve().parents[3]
PLAN_TOOLS = {"get_plan_history", "preview_plan_update", "apply_plan_update"}


def install(base_root: Path, transport_digest: str, *, enable_writes: bool = False,
            verification_root: Path | None = None, verification_id: str | None = None):
    base_root = base_root.resolve()
    transport = base_root / "protocol/v1/transport/mcp_stdio.py"
    if base_root == RELEASE_ROOT or hashlib.sha256(transport.read_bytes()).hexdigest() != transport_digest:
        raise ValueError("Pinned stdio transport does not match")
    if (verification_root is None) != (verification_id is None) or (verification_id and not verification_id.startswith("verification:")):
        raise ValueError("Verification requires an explicit namespace and isolated store")
    sys.path.insert(0, str(base_root))
    # Namespace fallbacks supply only modules absent from the installed runtime.
    for name in ("protocol", "protocol.v1", "protocol.v1.runtime"):
        package = importlib.import_module(name)
        package.__path__.append(str(RELEASE_ROOT.joinpath(*name.split("."))))
    from protocol.v1.runtime import advisory_plan_store, advisory_plan_update
    for module in (advisory_plan_store, advisory_plan_update):
        if Path(module.__file__).resolve().parent != RELEASE_ROOT / "protocol/v1/runtime":
            raise ValueError("Plan service must come from the identified extension release")
    from protocol.v1.security import trusted_approval
    trusted_approval._ACTION_PERMISSION["plan.update"] = "plan.update"
    from protocol.v1.adapters.gateway_facade import _public_data
    from protocol.v1.transport.stdio_rpc import _validate_schema
    spec = importlib.util.spec_from_file_location("investkitchen_native_plan_adapter", RELEASE_ROOT / "protocol/v1/adapters/advisory_state.py")
    adapter = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(adapter)
    spec = importlib.util.spec_from_file_location("investkitchen_pinned_stdio", transport)
    stdio = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(stdio)
    original_factory, original_catalog = stdio.build_reference_gateway, stdio.load_tool_catalog
    original_router = stdio.LocalToolRouter
    definitions = json.loads((RELEASE_ROOT / "protocol/v1/transport/tool_catalog.json").read_text())["tools"]
    additions = [row for row in definitions if row["name"] in PLAN_TOOLS]

    def catalog():
        value = copy.deepcopy(original_catalog())
        value["tools"] = [row for row in value["tools"] if row["name"] not in PLAN_TOOLS] + additions
        return value

    def factory(**kwargs):
        gateway = original_factory(**kwargs)
        root = kwargs.get("advisory_state_root")
        if root is None:
            return gateway
        store = advisory_plan_store.AdvisoryPlanStore(Path(root) / "plans")
        store.validate()
        stores = {}
        if verification_root is not None:
            verify = verification_root.resolve()
            protected = [base_root, RELEASE_ROOT, store.root]
            protected.extend(Path(kwargs[key]).resolve() for key in ("personal_data_root", "native_store_root", "approval_store_root") if kwargs.get(key))
            if (verification_id in gateway.portfolio_ids or any(verify == path or verify.is_relative_to(path)
                    or path.is_relative_to(verify) for path in protected)):
                raise ValueError("Verification namespace or storage is not isolated")
            stores[verification_id] = advisory_plan_store.AdvisoryPlanStore(verify)
            gateway.portfolio_ids = sorted({*gateway.portfolio_ids, verification_id})
        def select(portfolio_id):
            return stores.get(portfolio_id, store)
        gateway.handlers["plan.active"] = lambda payload: adapter.get_active_plans(select(payload["portfolio_id"]), portfolio_id=payload["portfolio_id"])
        gateway.plan_history_reader = lambda **payload: adapter.get_plan_history(select(payload["portfolio_id"]), **payload)
        if enable_writes and kwargs.get("write_principal") and kwargs.get("write_grant") and kwargs.get("approval_store_root"):
            grant = copy.deepcopy(kwargs["write_grant"])
            grant["permissions"] = sorted({*grant.get("permissions", []), "plan.update"})
            if grant.get("tool_allowlist"):
                grant["tool_allowlist"] = sorted({*grant["tool_allowlist"], "preview_plan_update", "apply_plan_update"})
            if verification_id:
                grant["portfolio_scope"] = sorted({*grant.get("portfolio_scope", []), verification_id})
            coordinator = advisory_plan_update.PlanUpdateCoordinator(store, trusted_approval.TrustedApprovalStore(kwargs["approval_store_root"]),
                kwargs["write_principal"], grant, portfolio_stores=stores)
            gateway.connector_handlers["preview_plan_update"] = coordinator.preview
            if "operation.approve" in grant["permissions"]:
                gateway.connector_handlers["apply_plan_update"] = coordinator.apply
        return gateway

    class PlanRouter(original_router):
        def _runtime_available(self, name):
            if name in {"preview_plan_update", "apply_plan_update"}:
                return self.gateway.connector_tool_ready(name)
            if name == "get_plan_history":
                return getattr(self.gateway, "plan_history_reader", None) is not None
            return super()._runtime_available(name)

        def call_tool(self, name, arguments):
            if name not in PLAN_TOOLS:
                return super().call_tool(name, arguments)
            if not self._runtime_available(name):
                return {"status": "blocked", "error_code": "plan_tool_unavailable"}
            _validate_schema(arguments, self.tools[name]["input_schema"])
            if name == "get_plan_history":
                return _public_data(self.gateway.plan_history_reader(**arguments))
            return self.gateway._invoke_connector(name, arguments)

    stdio.build_reference_gateway, stdio.load_tool_catalog, stdio.LocalToolRouter = factory, catalog, PlanRouter
    return stdio


def main():
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    parser.add_argument("--base-runtime", type=Path, required=True)
    parser.add_argument("--base-transport-digest", required=True)
    parser.add_argument("--enable-plan-writes", action="store_true")
    parser.add_argument("--plan-verification-root", type=Path)
    parser.add_argument("--plan-verification-id")
    args, rest = parser.parse_known_args()
    try:
        stdio = install(args.base_runtime, args.base_transport_digest, enable_writes=args.enable_plan_writes,
                        verification_root=args.plan_verification_root, verification_id=args.plan_verification_id)
        sys.argv = [str(args.base_runtime / "protocol/v1/transport/mcp_stdio.py"), *rest]
        return stdio.main()
    except Exception:
        sys.stderr.write("InvestKitchen Plan extension configuration error\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
