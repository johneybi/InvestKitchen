"""Optional bounded transport events, never request bodies or identities."""
from __future__ import annotations

import json
import os
import re
import stat
import threading
from datetime import datetime, timezone
from pathlib import Path


def private_transport_diagnostics(root: Path, protected: list[Path], tool_names: set[str]):
    resolved = root.resolve()
    if root.is_symlink() or any(
            resolved == path.resolve() or resolved.is_relative_to(path.resolve())
            or path.resolve().is_relative_to(resolved) for path in protected):
        raise ValueError("Diagnostics must be separate from source, configuration and stores")
    return TransportDiagnostics(resolved, tool_names)


class TransportDiagnostics:
    def __init__(self, root: Path, tool_names: set[str], *, max_bytes: int = 262144):
        if root.is_symlink():
            raise ValueError("Diagnostics require a private directory")
        info = root.stat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o700 or max_bytes < 1):
            raise ValueError("Diagnostics require a private owned directory")
        self.path = root / "transport-events.jsonl"
        self.tool_names = frozenset(tool_names)
        self.max_bytes = max_bytes
        self.lock = threading.Lock()
        if self.path.exists() or self.path.is_symlink():
            self._check_file(self.path.lstat())

    @staticmethod
    def _check_file(info):
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
            raise ValueError("Diagnostics require a private owned file")

    def _append(self, row):
        body = (json.dumps({"at": datetime.now(timezone.utc).isoformat(), **row},
                           separators=(",", ":")) + "\n").encode()
        try:
            with self.lock:
                fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                try:
                    info = os.fstat(fd)
                    self._check_file(info)
                    if info.st_size + len(body) <= self.max_bytes:
                        os.write(fd, body)
                finally:
                    os.close(fd)
        except (OSError, ValueError):
            # Optional diagnostics cannot authorize, block, or change a write.
            pass

    def http(self, method: str, protocol: str, has_session: bool, status: int, elapsed: float):
        self._append({"event": "http", "method": method if method in {"GET", "POST", "DELETE"} else "other",
                      "protocol": protocol if re.fullmatch(r"\d{4}-\d{2}-\d{2}", protocol) else "other",
                      "has_session_header": bool(has_session), "status": int(status),
                      "elapsed_ms": round(max(0, elapsed) * 1000)})

    def listed(self, count: int):
        self._append({"event": "tools_listed", "count": int(count)})

    def tool(self, name: str, outcome: str, elapsed: float = 0):
        allowed = {"started", "ok", "partial", "review_required", "applied", "blocked", "unavailable",
                   "error", "invalid_arguments", "not_authorized", "request_rejected", "internal_error", "other"}
        self._append({"event": "tool", "tool": name if name in self.tool_names else "unknown",
                      "outcome": outcome if isinstance(outcome, str) and outcome in allowed else "other",
                      "elapsed_ms": round(max(0, elapsed) * 1000)})
