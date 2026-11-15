#!/usr/bin/env python3
"""Build public end-user instructions; never read installed/private host state."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
CHATGPT = Path("deploy/chatgpt")
GEMINI = Path("deploy/gemini/skills/investkitchen")
SOURCE_IDS = ("operating_contract", "strategy_board", "presentation",
              "stock_analysis", "decision_reflection")


def digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def render(root: Path = ROOT) -> dict[Path, bytes]:
    release = json.loads((root / "client-instructions/release.json").read_text())
    manifest = json.loads((root / "protocol/v1/workflows/sources.json").read_text())
    inputs: dict[str, str] = {}

    def read(path: str) -> bytes:
        # All paths in release metadata must remain within the public source tree.
        relative = Path(path)
        resolved = (root / relative).resolve()
        if relative.is_absolute() or not resolved.is_relative_to(root.resolve()):
            raise ValueError("Instruction source must stay inside the product tree")
        raw = resolved.read_bytes()
        inputs[path] = digest(raw)
        return raw

    core = read(release["shared_source"]).decode()
    profiles = {name: read(path).decode() for name, path in release["profiles"].items()}
    banner = (f"배포 버전: {release['version']} · 방법론 호환 기준: "
              f"{release['methodology_version']}\n\n")
    frontmatter = ("---\nname: investkitchen\ndescription: 인베스트키친 계좌 조회, "
                   "근거 기반 주식 분석과 개인 투자 조언, 전략 보드, 행동 전 멘탈 점검과 "
                   "행동 후 복기, 지식·계획 등록에 사용하는 사용자 스킬.\n---\n\n")
    outputs = {
        CHATGPT / "project-instructions.ko.md": (banner + core + "\n" + profiles["chatgpt"]).encode(),
        GEMINI / "SKILL.md": (frontmatter + banner + core + "\n" + profiles["gemini"]).encode(),
    }
    for source_id in SOURCE_IDS:
        meta = manifest[source_id]
        filename = meta["filename"]
        if Path(filename).name != filename:
            raise ValueError("Methodology filename must be a plain filename")
        raw = read("protocol/v1/workflows/sources/" + filename)
        if digest(raw) != meta["sha256"]:
            raise ValueError("Methodology integrity mismatch: " + source_id)
        outputs[CHATGPT / "project-sources" / filename] = raw
        outputs[GEMINI / "references" / filename] = raw
    # Version metadata is part of drift detection, too.
    read("protocol/v1/workflows/sources.json")
    read("client-instructions/release.json")
    receipt = {
        **release,
        "source_sha256": inputs,
        "artifact_sha256": {path.as_posix(): digest(raw) for path, raw in sorted(outputs.items())},
        "installation_note": "Generated candidate; build success does not establish installation or model behavior.",
    }
    receipt_raw = (json.dumps(receipt, ensure_ascii=False, indent=2) + "\n").encode()
    outputs[CHATGPT / "release.json"] = receipt_raw
    outputs[GEMINI / "release.json"] = receipt_raw
    return outputs


def check(root: Path = ROOT) -> list[str]:
    expected = render(root)
    problems = [str(path) for path, raw in expected.items()
                if not (root / path).is_file() or (root / path).read_bytes() != raw]
    # Removed/renamed references must not silently survive a release.
    for directory in (CHATGPT / "project-sources", GEMINI / "references"):
        if (root / directory).exists():
            problems.extend(str(path.relative_to(root)) for path in (root / directory).rglob("*")
                            if path.is_file() and path.relative_to(root) not in expected)
    return sorted(problems)


def write_zip(target: Path, outputs: dict[Path, bytes]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, raw in sorted(outputs.items()):
            if path.is_relative_to(GEMINI):
                member = "investkitchen/" + path.relative_to(GEMINI).as_posix()
                info = zipfile.ZipInfo(member, date_time=(2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o100644 << 16
                archive.writestr(info, raw)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Fail on generated artifact drift; write nothing")
    parser.add_argument("--zip", type=Path, help="Optional Gemini upload bundle (folder/SKILL.md also supported)")
    args = parser.parse_args()
    if args.check and args.zip:
        parser.error("--check cannot write an archive")
    outputs = render()
    if args.check:
        problems = check()
        if problems:
            print("CLIENT INSTRUCTIONS DRIFT: " + ", ".join(problems))
            return 1
    else:
        for path, raw in outputs.items():
            (ROOT / path).parent.mkdir(parents=True, exist_ok=True)
            (ROOT / path).write_bytes(raw)
        if check():
            raise ValueError("Stale generated references remain; review before removing them")
        if args.zip:
            write_zip(args.zip, outputs)
    print(f"CLIENT INSTRUCTIONS {'CHECK' if args.check else 'BUILD'} PASS: {len(outputs)} files; host verification pending")
    return 0


if __name__ == "__main__":
    sys.exit(main())
