from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


MANIFEST_NAME = "personal-data-manifest.json"


class PersonalDataError(RuntimeError):
    pass


class PersonalDataIntegrityError(PersonalDataError):
    pass


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_relative(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise PersonalDataIntegrityError("personal_data_path_invalid")
    return path


def _read_required_file(root: Path, relative: Path) -> bytes:
    path = root / relative
    try:
        if path.is_symlink():
            raise PersonalDataIntegrityError("personal_data_symlink_not_allowed")
        if not path.is_file():
            raise PersonalDataIntegrityError("personal_data_file_missing")
        return path.read_bytes()
    except OSError as exc:
        raise PersonalDataIntegrityError("personal_data_file_read_failed") from exc


def _read_json_bytes(value: bytes, *, code: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PersonalDataIntegrityError(code) from exc
    if not isinstance(parsed, dict):
        raise PersonalDataIntegrityError(code)
    return parsed


@dataclass(frozen=True)
class PersonalDataStore:
    root: Path

    def resolved_root(self) -> Path:
        return self.root.expanduser().resolve()

    def load_manifest(self) -> dict[str, Any]:
        root = self.resolved_root()
        value = _read_json_bytes(
            _read_required_file(root, Path(MANIFEST_NAME)),
            code="personal_data_manifest_invalid",
        )
        if value.get("protocol_version") != "1.0-draft" or value.get("bundle_format_version") != 1:
            raise PersonalDataIntegrityError("personal_data_manifest_version_unsupported")
        return value

    def verify(self) -> dict[str, Any]:
        root = self.resolved_root()
        manifest = self.load_manifest()
        files = manifest.get("files")
        if not isinstance(files, list) or not files:
            raise PersonalDataIntegrityError("personal_data_manifest_files_invalid")
        seen_names: set[str] = set()
        seen_paths: set[str] = set()
        portfolio_files: dict[str, str] = {}
        has_current = False
        has_projection = False
        for row in files:
            if not isinstance(row, dict):
                raise PersonalDataIntegrityError("personal_data_manifest_files_invalid")
            logical_name = str(row.get("logical_name") or "")
            relative_text = str(row.get("relative_path") or "")
            if not logical_name or logical_name in seen_names or relative_text in seen_paths:
                raise PersonalDataIntegrityError("personal_data_manifest_duplicate_file")
            seen_names.add(logical_name)
            seen_paths.add(relative_text)
            relative = _safe_relative(relative_text)
            content = _read_required_file(root, relative)
            if row.get("sha256") != _sha256(content) or row.get("size_bytes") != len(content):
                raise PersonalDataIntegrityError("personal_data_file_digest_mismatch")
            parsed = _read_json_bytes(content, code="personal_data_json_invalid")
            content_type = row.get("content_type")
            if content_type == "portfolio":
                portfolio_id = str(row.get("portfolio_id") or "")
                if not portfolio_id or parsed.get("portfolio_id") != portfolio_id:
                    raise PersonalDataIntegrityError("personal_data_portfolio_identity_mismatch")
                portfolio_files[portfolio_id] = relative_text
            elif content_type == "knowledge_current":
                if parsed.get("capability") != "knowledge.current":
                    raise PersonalDataIntegrityError("personal_data_knowledge_current_invalid")
                has_current = True
            elif content_type == "knowledge_projection":
                if not isinstance(parsed.get("claims"), list) or not isinstance(parsed.get("evidence"), list):
                    raise PersonalDataIntegrityError("personal_data_knowledge_projection_invalid")
                has_projection = True
            else:
                raise PersonalDataIntegrityError("personal_data_content_type_unknown")

        expected_portfolios = {str(value) for value in manifest.get("portfolio_ids") or []}
        if set(portfolio_files) != expected_portfolios:
            raise PersonalDataIntegrityError("personal_data_portfolio_set_mismatch")
        if not has_current or not has_projection:
            raise PersonalDataIntegrityError("personal_data_knowledge_files_missing")
        return {
            "ok": True,
            "bundle_id": manifest.get("bundle_id"),
            "portfolio_ids": sorted(expected_portfolios),
            "file_count": len(files),
        }

    def _entry(self, *, content_type: str, portfolio_id: str | None = None) -> dict[str, Any]:
        manifest = self.load_manifest()
        matches = [
            row for row in manifest.get("files") or []
            if isinstance(row, dict)
            and row.get("content_type") == content_type
            and (portfolio_id is None or row.get("portfolio_id") == portfolio_id)
        ]
        if len(matches) != 1:
            raise PersonalDataIntegrityError("personal_data_entry_missing_or_ambiguous")
        return matches[0]

    def _load_entry(self, row: dict[str, Any]) -> dict[str, Any]:
        root = self.resolved_root()
        relative = _safe_relative(str(row["relative_path"]))
        content = _read_required_file(root, relative)
        if row.get("sha256") != _sha256(content) or row.get("size_bytes") != len(content):
            raise PersonalDataIntegrityError("personal_data_file_digest_mismatch")
        return _read_json_bytes(content, code="personal_data_json_invalid")

    def portfolio(self, portfolio_id: str) -> dict[str, Any]:
        return self._load_entry(self._entry(content_type="portfolio", portfolio_id=portfolio_id))

    def knowledge_current(self) -> dict[str, Any]:
        return self._load_entry(self._entry(content_type="knowledge_current"))

    def knowledge_projection(self) -> dict[str, Any]:
        return self._load_entry(self._entry(content_type="knowledge_projection"))
