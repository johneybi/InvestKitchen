"""Guard exact-generation content against exposing filesystem locators."""
from __future__ import annotations

import re
from typing import Any


_LOCAL_LOCATOR = re.compile(r"(?:^|[\s\"'`(])(?:file://|~/|/(?!/)|[A-Za-z]:[\\/])")
_INTERNAL_KEYS = {"source_path", "canonical_path", "normalized_path", "workspace",
                  "approval_verification_ref", "credential", "client_secret", "access_token",
                  "refresh_token", "password", "raw_journal"}


def contains_private_locator(value: Any) -> bool:
    if isinstance(value, dict):
        return bool(_INTERNAL_KEYS & value.keys()) or any(contains_private_locator(item) for item in value.values())
    if isinstance(value, list):
        return any(contains_private_locator(item) for item in value)
    return isinstance(value, str) and _LOCAL_LOCATOR.search(value) is not None
