from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from protocol.v1.adapters.common import digest, timepoint  # noqa: E402
from protocol.v1.providers.account_readonly_adapters import (  # noqa: E402
    normalize_namuh_account_snapshot,
    normalize_toss_account_snapshot,
)


TOSS_BASE_URL = "https://openapi.tossinvest.com"
NH_LIVE_BASE_URL = "https://api.nhplug.com:8443"
NH_MOCK_BASE_URL = "https://moapi.nhplug.com:8443"
MAX_RESPONSE_BYTES = 2_000_000


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _load_secret(path: Path, provider_id: str) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("account provider secret binding is invalid")
    if provider_id == "toss_securities":
        if value.get("provider_id") not in (None, provider_id):
            raise ValueError("account provider secret binding is invalid")
        credentials = value.get("credentials")
        has_default = all(isinstance(value.get(field), str) and value[field] for field in ("client_id", "client_secret"))
        has_profiles = isinstance(credentials, dict) and any(
            isinstance(profile, dict)
            and all(isinstance(profile.get(field), str) and profile[field] for field in ("client_id", "client_secret"))
            for profile in credentials.values()
        )
        if not has_default and not has_profiles:
            raise ValueError("toss account credential is incomplete")
    elif provider_id == "nhplug":
        if value.get("provider_id") != provider_id:
            raise ValueError("account provider secret binding is invalid")
        accounts = value.get("accounts")
        if not isinstance(accounts, dict) or not accounts:
            raise ValueError("account provider secret has no account bindings")
        for field in ("app_key", "app_secret"):
            if not isinstance(value.get(field), str) or not value[field]:
                raise ValueError("nhplug account credential is incomplete")
    else:
        raise ValueError("unsupported account provider")
    return value


def _account_secret(secret: Mapping[str, Any], binding: Mapping[str, Any]) -> dict[str, Any]:
    provider_ref = str(binding.get("provider_account_ref") or "")
    accounts = secret.get("accounts")
    raw = accounts.get(provider_ref) if isinstance(accounts, Mapping) else None
    if not isinstance(raw, dict):
        raise ValueError("provider account binding not found")
    return raw


def _toss_credential(secret: Mapping[str, Any], binding: Mapping[str, Any]) -> dict[str, Any]:
    credential_ref = str(binding.get("provider_credential_ref") or "").strip()
    if credential_ref:
        credentials = secret.get("credentials")
        raw = credentials.get(credential_ref) if isinstance(credentials, Mapping) else None
        if not isinstance(raw, dict):
            raise ValueError("toss credential profile not found")
        for field in ("client_id", "client_secret"):
            if not isinstance(raw.get(field), str) or not raw[field]:
                raise ValueError("toss account credential is incomplete")
        return raw
    for field in ("client_id", "client_secret"):
        if not isinstance(secret.get(field), str) or not secret[field]:
            raise ValueError("toss account credential is incomplete")
    return dict(secret)


def _request_json(
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    data: bytes | None = None,
    timeout: float = 10.0,
) -> tuple[Any, dict[str, str]]:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "investkitchen-account-readonly/1", **dict(headers or {})},
        data=data,
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
            response_headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise RuntimeError("account_provider_auth_unavailable") from None
        if exc.code == 429:
            raise RuntimeError("account_provider_rate_limited") from None
        raise RuntimeError("account_provider_http_unavailable") from None
    except Exception:
        raise RuntimeError("account_provider_http_unavailable") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise RuntimeError("account_provider_response_too_large")
    try:
        return json.loads(raw.decode("utf-8")), response_headers
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise RuntimeError("account_provider_payload_invalid") from None


def _toss_access_token(secret: Mapping[str, Any]) -> str:
    payload = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": str(secret["client_id"]),
        "client_secret": str(secret["client_secret"]),
    }).encode("utf-8")
    body, _ = _request_json(
        TOSS_BASE_URL + "/oauth2/token",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data=payload,
    )
    token = body.get("access_token") if isinstance(body, dict) else None
    if not isinstance(token, str) or not token:
        raise RuntimeError("account_provider_auth_unavailable")
    return token


def _toss_snapshot(binding: Mapping[str, Any], secret: Mapping[str, Any]) -> dict[str, Any]:
    credential = _toss_credential(secret, binding)
    token = _toss_access_token(credential)
    auth = {"Authorization": "Bearer " + token}
    account_body, _ = _request_json(TOSS_BASE_URL + "/api/v1/accounts", headers=auth)
    rows = account_body.get("result") if isinstance(account_body, dict) else None
    if not isinstance(rows, list):
        raise RuntimeError("account_provider_payload_invalid")
    provider_ref = str(binding.get("provider_account_ref") or "")
    if provider_ref == "unique_brokerage":
        matching = [row for row in rows if isinstance(row, dict) and row.get("accountType") == "BROKERAGE"]
    else:
        account = _account_secret(credential, binding)
        account_seq = account.get("account_seq")
        if isinstance(account_seq, bool) or not isinstance(account_seq, int) or account_seq < 1:
            raise ValueError("toss provider account_seq is invalid")
        matching = [row for row in rows if isinstance(row, dict) and row.get("accountSeq") == account_seq]
    if len(matching) != 1 or matching[0].get("accountType") != "BROKERAGE":
        raise RuntimeError("account_provider_binding_mismatch")
    account_seq = matching[0].get("accountSeq")
    if isinstance(account_seq, bool) or not isinstance(account_seq, int) or account_seq < 1:
        raise RuntimeError("account_provider_payload_invalid")

    completed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    holdings_body, headers = _request_json(
        TOSS_BASE_URL + "/api/v1/holdings",
        headers={**auth, "X-Tossinvest-Account": str(account_seq)},
    )
    request_id = str(headers.get("x-request-id") or "").strip()
    if not request_id:
        raise RuntimeError("account_provider_observation_ref_missing")
    return normalize_toss_account_snapshot(
        {
            **(holdings_body if isinstance(holdings_body, dict) else {}),
            "observed_at": timepoint(completed_at),
            "retrieved_at": timepoint(completed_at),
            "source_ref": "provider-observation:toss:" + request_id,
            # The current holdings contract does not expose nominal cash.
            # Keep cash unavailable instead of substituting buying power.
            "cash": [],
            "completeness": {"holdings": "complete", "cash": "unavailable"},
        },
        binding,
    )


def _configure_nhplug(secret: Mapping[str, Any], account: Mapping[str, Any]) -> None:
    acct_type = str(account.get("acct_type") or "")
    if acct_type not in {"01", "02", "03"}:
        raise ValueError("nhplug account type is invalid")
    expected_base = NH_MOCK_BASE_URL if acct_type == "03" else NH_LIVE_BASE_URL
    configured_base = str(secret.get("base_url") or expected_base).rstrip("/")
    if configured_base != expected_base:
        raise ValueError("nhplug account type and data host do not match")
    configured_auth = str(secret.get("auth_url") or NH_LIVE_BASE_URL).rstrip("/")
    if configured_auth != NH_LIVE_BASE_URL:
        raise ValueError("nhplug auth host is invalid")
    os.environ["NHPLUG_APP_KEY"] = str(secret["app_key"])
    os.environ["NHPLUG_APP_SECRET"] = str(secret["app_secret"])
    os.environ["NHPLUG_BASE_URL"] = configured_base
    os.environ["NHPLUG_AUTH_URL"] = configured_auth
    os.environ["NHPLUG_TOKEN_CACHE"] = "0"


def _nhplug_snapshot(binding: Mapping[str, Any], secret: Mapping[str, Any]) -> dict[str, Any]:
    account = _account_secret(secret, binding)
    act_no = str(account.get("act_no") or "").strip()
    acct_type = str(account.get("acct_type") or "").strip()
    if not act_no:
        raise ValueError("nhplug account number is invalid")
    _configure_nhplug(secret, account)
    try:
        from nhplug import call, paginate  # type: ignore
    except ImportError as exc:
        raise RuntimeError("account_provider_dependency_unavailable") from exc

    listed = call("/n2/acctinfo", {})
    list_rows = listed.get("Output_0") if isinstance(listed, dict) else None
    if not isinstance(list_rows, list):
        raise RuntimeError("account_provider_payload_invalid")
    matching = [
        row for row in list_rows
        if isinstance(row, dict)
        and str(row.get("acct_no") or "") == act_no
        and str(row.get("acct_type") or "") == acct_type
    ]
    if len(matching) != 1:
        raise RuntimeError("account_provider_binding_mismatch")

    params = {
        "act_no": act_no,
        "bnc_bse_cd": "5",
        "ltg_aot_dit_cd": "9",
        "aet_bse": "2",
        "qut_dit_cd": "UNT",
        "aly_qut_cd": "1",
    }
    pages = list(paginate("/krstock/inquiry/v1/balance", params))
    if not pages or any(not isinstance(page, dict) for page in pages):
        raise RuntimeError("account_provider_payload_invalid")
    first_output = pages[0].get("Output_0")
    if isinstance(first_output, list):
        first_output = first_output[0] if len(first_output) == 1 and isinstance(first_output[0], dict) else None
    if not isinstance(first_output, dict):
        raise RuntimeError("account_provider_payload_invalid")
    positions: list[dict[str, Any]] = []
    for page in pages:
        page_positions = page.get("Output_1")
        if not isinstance(page_positions, list):
            raise RuntimeError("account_provider_payload_invalid")
        for row in page_positions:
            if not isinstance(row, dict):
                raise RuntimeError("account_provider_payload_invalid")
            positions.append(row)
    if "dca" not in first_output and "dnca_tot_amt" not in first_output:
        raise RuntimeError("account_provider_cash_unavailable")
    completed_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    observation_digest = digest({"Output_0": first_output, "Output_1": positions})[:24]
    return normalize_namuh_account_snapshot(
        {
            "observed_at": timepoint(completed_at),
            "retrieved_at": timepoint(completed_at),
            "source_ref": "provider-observation:nhplug:" + observation_digest,
            "value": {"Output_0": first_output, "Output_1": positions},
            "completeness": {"holdings": "complete", "cash": "complete"},
        },
        binding,
    )


def read_account_snapshot(binding: Mapping[str, Any], *, provider_id: str, secret_file: Path) -> dict[str, Any]:
    if not isinstance(binding, Mapping) or binding.get("provider_id") != provider_id:
        raise ValueError("account provider binding is invalid")
    secret = _load_secret(secret_file, provider_id)
    if provider_id == "toss_securities":
        return _toss_snapshot(binding, secret)
    if provider_id == "nhplug":
        return _nhplug_snapshot(binding, secret)
    raise ValueError("unsupported account provider")


def main() -> int:
    parser = argparse.ArgumentParser(description="Isolated read-only account provider worker")
    parser.add_argument("--provider-id", choices=("toss_securities", "nhplug"), required=True)
    parser.add_argument("--secret-file", type=Path, required=True)
    args = parser.parse_args()
    try:
        request = json.loads(sys.stdin.read())
        binding = request.get("binding") if isinstance(request, dict) else None
        if not isinstance(binding, dict):
            raise ValueError("account provider request is invalid")
        result = read_account_snapshot(binding, provider_id=args.provider_id, secret_file=args.secret_file)
        sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
        return 0
    except Exception:
        # Never serialize credentials, account numbers or raw provider failures.
        sys.stderr.write("account provider unavailable\n")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
