"""CLI double. Core's real shell scripts invoke this executable in tests."""

import json
from pathlib import Path
import re
import sys

root = Path(__file__).parent
state_path = root / "docker.json"
state = json.loads(state_path.read_text()) if state_path.exists() else {"images": {}, "container": None}
args = sys.argv[1:]
with (root / "docker.log").open("a") as log:
    print(json.dumps(args), file=log)

match args:
    case ["load", "-i", archive]:
        image, identity, revision = Path(archive).read_text().split()
        state["images"][image] = f"{identity} linux/amd64 {revision}"
    case ["image", "inspect", "--format", template, image]:
        if image not in state["images"]:
            sys.exit(1)
        if template == "{{.Id}}":
            print(state["images"][image].split()[0])
        elif template == "{{.Architecture}}/{{.Os}}":
            print("amd64/linux")
        elif template.startswith("{{index"):
            print(state["images"][image].split()[-1])
        else:
            print(state["images"][image])
    case ["image", "inspect", image]:
        sys.exit(0 if image in state["images"] else 1)
    case ["run", *rest] if "--version" in rest:
        print("0.0.14")
    case ["run", *rest] if "preflight" in rest:
        print(json.dumps({"ok": not (root / "fail-core-preflight").exists(), "tools": [
            "get_capabilities", "get_portfolio_state", "get_open_items", "get_current_policy",
            "get_active_plans", "get_current_knowledge", "search_knowledge", "build_decision_context",
            "start_reflection", "get_decision_history", "get_transactions", "get_market_quote", "get_market_ohlcv",
        ]}))
    case ["compose", *rest]:
        env = Path(rest[rest.index("--env-file") + 1]).read_text()
        command = rest[rest.index("-f") + 2]
        if command == "up":
            image = re.search(r"^(?:MARKET_EDITION_IMAGE|TRADEMIND_RUNTIME_IMAGE_REF)=(.+)$",
                              env, re.MULTILINE)[1]
            failing = (root / "fail-cutover").exists() and (root / "fail-cutover").read_text() in image
            state["container"] = {"image": image, "status": ("exited" if failing else
                                                            "healthy" if rest[-1] == "runtime" else "running")}
        elif command == "ps" and state["container"]:
            print("synthetic-container")
        elif command == "stop" and state["container"]:
            state["container"]["status"] = "exited"
    case ["inspect", "--format", template, "synthetic-container"]:
        active = state["container"]
        if template == "{{.Config.Image}}":
            print(active["image"])
        elif template.startswith("{{.Image}}"):
            print(state["images"][active["image"]].split()[0], active["status"])
        else:
            print(active["status"])
    case ["exec", "synthetic-container", "python", "-c", probe] if "TLSv1_3" in probe:
        pass
    case _:
        sys.exit(2)

state_path.write_text(json.dumps(state))
