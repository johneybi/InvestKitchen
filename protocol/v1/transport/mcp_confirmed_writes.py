"""OAuth-bound native writes with a separate authenticated human review.

MCP annotations and model-supplied confirmation strings are not proof of human
approval. The browser authenticates independently through a loopback Authelia
CookieSession check. No bearer token or caller-supplied identity header is sent
to that check. Pending approvals are bounded, process-local and single-use.
"""
from __future__ import annotations

import copy
import hashlib
import html
import http.client
import json
import os
import secrets
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal
from urllib.parse import parse_qs, quote, urlsplit

import anyio
from jsonschema import Draft202012Validator, ValidationError
from mcp.server.auth.provider import AccessToken
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response
from starlette.routing import Route

from protocol.v1.security.oauth_resource import OAuthResourceConfig, READ_SCOPE
from protocol.v1.transport.mcp_read_surface import ReadSurfaceError
from protocol.v1.transport.stdio_rpc import LocalToolRouter


WRITE_SCOPE = "investkitchen.write"
WRITE_TOOLS = {
    "preview_knowledge_update": ("knowledge.commit", "apply_knowledge_update"),
    "preview_portfolio_update": ("portfolio.update", "apply_portfolio_update"),
    "preview_plan_update": ("plan.update", "apply_plan_update"),
}
APPLY_TOOLS = {apply: preview for preview, (_, apply) in WRITE_TOOLS.items()}
COOKIE = "__Host-investkitchen-review"


class WriteGrant(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    subject: str = Field(min_length=1)
    client_id: str = Field(min_length=1)
    browser_username: str = Field(min_length=1, max_length=256)
    permissions: tuple[Literal["knowledge.commit", "portfolio.update", "plan.update"], ...] = Field(min_length=1)
    portfolio_scope: tuple[str, ...] = ()
    expires_at: datetime

    @field_validator("expires_at")
    @classmethod
    def zoned_expiry(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Write grant expiry requires a timezone")
        return value

    @model_validator(mode="after")
    def bounded_grant(self):
        if (len(set(self.permissions)) != len(self.permissions)
                or len(set(self.portfolio_scope)) != len(self.portfolio_scope)
                or any(not value.strip() for value in self.portfolio_scope)
                or self.browser_username != self.browser_username.strip()):
            raise ValueError("Invalid write grant")
        if {"portfolio.update", "plan.update"} & set(self.permissions) and not self.portfolio_scope:
            raise ValueError("Portfolio writes require an explicit scope")
        return self

    @property
    def identity(self) -> tuple[str, str]:
        return self.subject, self.client_id


class ConfirmedWriteConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    authelia_authz_url: str
    authelia_cookie_name: str = Field(default="authelia_session", pattern=r"^[A-Za-z0-9_-]{1,64}$")
    grants: tuple[WriteGrant, ...] = Field(min_length=1)

    @field_validator("authelia_authz_url")
    @classmethod
    def loopback_authz(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
                or not parsed.port or parsed.path != "/api/authz/forward-auth"
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Approval authentication must use the loopback forward-auth endpoint")
        return value

    @model_validator(mode="after")
    def unique_identities(self):
        if len({g.identity for g in self.grants}) != len(self.grants):
            raise ValueError("Duplicate write identity")
        return self

    def validate_read_grants(self, resource: OAuthResourceConfig) -> None:
        for grant in self.grants:
            read = resource.grant_for(*grant.identity)
            if read is None or not set(grant.portfolio_scope).issubset(read.portfolio_scope):
                raise ValueError("Writes must remain inside the corresponding read grant")
            for permission, required in [("knowledge.commit", "knowledge.read"),
                                         ("portfolio.update", "portfolio.read"), ("plan.update", "plan.read")]:
                if permission in grant.permissions and required not in read.permissions:
                    raise ValueError("Writes require the corresponding read permission")


class AutheliaBrowserIdentity:
    """Validate only a browser CookieSession against a fixed trusted service."""

    def __init__(self, config: ConfirmedWriteConfig, origin: str):
        self.config = config
        self.origin = origin

    def __call__(self, request: Request) -> str | None:
        cookie = request.cookies.get(self.config.authelia_cookie_name)
        if not cookie:
            return None
        if len(cookie) > 16384 or any(ch in cookie for ch in "\r\n;"):
            return None
        endpoint = urlsplit(self.config.authelia_authz_url)
        conn = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=5)
        try:
            conn.request("GET", endpoint.path, headers={
                "Cookie": self.config.authelia_cookie_name + "=" + cookie,
                "X-Forwarded-Method": request.method,
                "X-Forwarded-Proto": "https",
                "X-Forwarded-Host": urlsplit(self.origin).netloc,
                "X-Forwarded-URI": request.url.path,
                "X-Forwarded-For": "127.0.0.1",
            })
            response = conn.getresponse()
            username = response.getheader("Remote-User", "")
            if response.status == 200 and username and len(username) <= 256:
                return username
            if response.status in {200, 302, 303, 401, 403}:
                return None
            raise ReadSurfaceError("Approval authentication unavailable")
        except (OSError, http.client.HTTPException):
            raise ReadSurfaceError("Approval authentication unavailable") from None
        finally:
            conn.close()


class ConfirmedWriteSurface:
    def __init__(self, resource: OAuthResourceConfig, config: ConfirmedWriteConfig,
                 gateway_factory: Callable[[WriteGrant], Any], *, audit_root: Path,
                 browser_identity: Callable[[Request], str | None] | None = None,
                 clock: Callable[[], float] = time.time, max_pending: int = 100):
        config.validate_read_grants(resource)
        if max_pending < 1:
            raise ValueError("Pending review capacity must be positive")
        self.resource = resource
        self.config = config
        self.origin = "https://" + urlsplit(resource.resource_url).netloc
        self.clock = clock
        self.max_pending = max_pending
        self.audit_root = audit_root
        self.browser_identity = browser_identity or AutheliaBrowserIdentity(config, self.origin)
        self.lock = threading.RLock()
        self.pending: dict[str, dict[str, Any]] = {}
        self.sessions: dict[str, dict[str, Any]] = {}
        self.backends = {}
        for grant in config.grants:
            gateway = gateway_factory(grant)
            if gateway.connector_review is None or gateway.connector_discard is None:
                raise ValueError("Write backend must support exact review and expiry cleanup")
            self.backends[grant.identity] = (gateway, LocalToolRouter(gateway))

    def grant(self, token: AccessToken | None) -> WriteGrant:
        if (token is None or token.resource != self.resource.resource_url
                or not {READ_SCOPE, WRITE_SCOPE}.issubset(token.scopes)
                or token.expires_at is None or token.expires_at <= self.clock()):
            raise ReadSurfaceError("Write authorization required")
        for grant in self.config.grants:
            if grant.identity == (token.subject, token.client_id) and grant.expires_at.timestamp() > self.clock():
                return grant
        raise ReadSurfaceError("Write authorization required")

    def names(self, grant: WriteGrant) -> set[str]:
        _, router = self.backends[grant.identity]
        names = set()
        ready = router.available_tool_names()
        for preview, (permission, apply) in WRITE_TOOLS.items():
            if permission in grant.permissions and {preview, apply}.issubset(ready):
                names.update([preview, apply])
        return names

    def list_tools(self, token: AccessToken | None) -> list[dict[str, Any]]:
        try:
            grant = self.grant(token)
        except ReadSurfaceError:
            return []
        _, router = self.backends[grant.identity]
        return [{"name": name,
                 "description": router.tools[name]["description"] + (
                     " Open approval_url and review the exact change in the authenticated NAS browser before apply."
                     if name in WRITE_TOOLS else
                     " Requires the separate NAS browser approval; confirmation text alone cannot authorize this call."),
                 "inputSchema": copy.deepcopy(router.tools[name]["input_schema"]),
                 "annotations": {"readOnlyHint": name in WRITE_TOOLS,
                                 "destructiveHint": name in APPLY_TOOLS,
                                 "idempotentHint": False, "openWorldHint": False}}
                for name in sorted(self.names(grant))]

    def _prune(self):
        for ticket, pending in list(self.pending.items()):
            if pending["expires"] <= self.clock():
                self.backends[pending["identity"]][0].connector_discard(pending["preview_id"])
                del self.pending[ticket]
        for sid, session in list(self.sessions.items()):
            if session["expires"] <= self.clock():
                del self.sessions[sid]

    def call_tool(self, token: AccessToken | None, name: str, arguments: dict[str, Any]) -> Any:
        with self.lock:
            self._prune()
            grant = self.grant(token)
            if name not in self.names(grant):
                raise ReadSurfaceError("Unknown or unavailable write tool")
            gateway, router = self.backends[grant.identity]
            try:
                Draft202012Validator(router.tools[name]["input_schema"]).validate(arguments)
            except ValidationError:
                raise ReadSurfaceError("Invalid tool arguments") from None
            arguments = copy.deepcopy(arguments)
            if name in WRITE_TOOLS:
                if len(self.pending) >= self.max_pending:
                    raise ReadSurfaceError("Pending review limit reached")
                if name in {"preview_portfolio_update", "preview_plan_update"} and arguments["update"]["portfolio_id"] not in grant.portfolio_scope:
                    raise ReadSurfaceError("Requested portfolio is not authorized")
                result = router.call_tool(name, arguments)
                if result.get("status") != "review_required":
                    return result
                preview_id = result["preview_id"]
                try:
                    review = gateway.connector_review(preview_id)
                except Exception:
                    gateway.connector_discard(preview_id)
                    raise ReadSurfaceError("Exact write review unavailable") from None
                ticket = secrets.token_urlsafe(32)
                self.pending[ticket] = {
                    "identity": grant.identity, "preview_id": preview_id,
                    "apply": WRITE_TOOLS[name][1], "confirmation": result["confirmation"],
                    "review": review,
                    "expires": min(self.clock() + 300, grant.expires_at.timestamp()),
                    "state": "awaiting_approval", "browser_session": None,
                }
                return {**result, "approval_url": self.origin + "/approve/" + ticket,
                        "approval_state": "awaiting_authenticated_browser_approval"}
            pending = next((p for p in self.pending.values() if p["identity"] == grant.identity
                            and p["preview_id"] == arguments["preview_id"] and p["apply"] == name), None)
            if pending is None:
                raise ReadSurfaceError("Preview not found or expired")
            if pending["state"] != "approved":
                raise ReadSurfaceError("Authenticated browser approval required")
            if not secrets.compare_digest(arguments["confirmation"], pending["confirmation"]):
                raise ReadSurfaceError("Confirmation mismatch")
            pending["state"] = "consumed"
            try:
                result = router.call_tool(name, arguments)
                pending["state"] = "applied" if result.get("status") == "applied" else "failed"
                return result
            finally:
                gateway.connector_discard(pending["preview_id"])

    def _audit(self, ticket: str, pending: dict[str, Any], username: str, decision: str):
        self.audit_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        row = {"event": "authenticated_browser_" + decision,
               "subject": pending["identity"][0], "client_id": pending["identity"][1],
               "browser_username": username, "preview_id": pending["preview_id"],
               "review_digest": hashlib.sha256(json.dumps(pending["review"], sort_keys=True).encode()).hexdigest(),
               "interaction_ref": hashlib.sha256(ticket.encode()).hexdigest(), "at": self.clock()}
        fd = os.open(self.audit_root / "browser-confirmations.jsonl", os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        try:
            data = (json.dumps(row, ensure_ascii=False) + "\n").encode()
            with os.fdopen(fd, "ab") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
        except Exception:
            raise ReadSurfaceError("Approval evidence could not be recorded") from None

    @staticmethod
    def _html(body: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse('<!doctype html><html lang="ko"><meta charset="utf-8">'
                            '<meta name="viewport" content="width=device-width,initial-scale=1">'
                            '<title>InvestKitchen 변경 승인</title><style>'
                            'body{font:16px/1.6 system-ui;max-width:900px;margin:40px auto;padding:0 20px}'
                            'pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f5f7;padding:20px}'
                            'button{font:inherit;padding:12px 18px;margin:8px;cursor:pointer}'
                            '</style><h1>InvestKitchen 변경 승인</h1>' + body + '</html>', status_code=status,
                            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
                                     "X-Content-Type-Options": "nosniff",
                                     "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'"})

    async def browser_review(self, request: Request) -> Response:
        if request.headers.get("host") != urlsplit(self.origin).netloc:
            return self._html("요청 주소를 확인하세요.", 400)
        try:
            username = await anyio.to_thread.run_sync(self.browser_identity, request)
        except Exception:
            return self._html("인증 서비스를 확인할 수 없습니다. 변경은 승인되지 않았습니다.", 503)
        ticket = request.path_params["ticket"]
        if username is None:
            if request.method == "POST":
                return self._html("로그인 후 변경 내용을 다시 확인하세요.", 401)
            return RedirectResponse(self.origin + "/?rd=" + quote(self.origin + "/approve/" + ticket, safe=""),
                                    status_code=303, headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"})
        form = None
        if request.method == "POST":
            if request.headers.get("origin") != self.origin or request.headers.get("content-type", "").split(";")[0] != "application/x-www-form-urlencoded":
                return self._html("승인 요청을 확인할 수 없습니다.", 403)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 4096:
                    return self._html("승인 요청이 너무 큽니다.", 413)
            try:
                form = parse_qs(body.decode(), max_num_fields=4, strict_parsing=True)
            except (ValueError, UnicodeError):
                return self._html("올바르지 않은 승인 요청입니다.", 400)
        with self.lock:
            self._prune()
            pending = self.pending.get(ticket)
            grant = next((g for g in self.config.grants if pending and g.identity == pending["identity"]), None)
            if pending is None or grant is None or username != grant.browser_username:
                return self._html("요청을 찾을 수 없거나 접근 권한이 없습니다.", 404)
            if grant.expires_at.timestamp() <= self.clock():
                return self._html("쓰기 권한이 만료됐습니다.", 403)
            if pending["state"] != "awaiting_approval":
                return self._html("이 요청은 이미 처리됐습니다. Gemini에서 현재 결과를 확인하세요.", 409)
            sid = request.cookies.get(COOKIE, "")
            session = self.sessions.get(sid)
            if request.method == "POST":
                if (session is None or session["username"] != username or pending["browser_session"] != sid
                        or set(form or {}) != {"csrf", "decision"}
                        or len(form["csrf"]) != 1 or len(form["decision"]) != 1
                        or not secrets.compare_digest(form["csrf"][0], session["csrf"])
                        or form["decision"][0] not in {"approve", "reject"}):
                    return self._html("승인 세션을 확인할 수 없습니다. 페이지를 다시 여세요.", 403)
                decision = form["decision"][0]
                try:
                    self._audit(ticket, pending, username, decision)
                except Exception:
                    return self._html("승인 기록을 저장하지 못했습니다. 변경은 승인되지 않았습니다.", 503)
                pending["state"] = "approved" if decision == "approve" else "rejected"
                if decision == "reject":
                    self.backends[pending["identity"]][0].connector_discard(pending["preview_id"])
                    return self._html("변경을 거절했습니다. 저장하지 않았습니다.")
                return self._html("<p>변경을 승인했습니다. 아직 저장하지 않았습니다.</p><p>Gemini 대화로 돌아가 이 미리보기의 적용을 요청하세요.</p>")
            if session is None or session["username"] != username:
                if len(self.sessions) >= self.max_pending * 2:
                    return self._html("승인 세션이 많습니다. 잠시 후 다시 시도하세요.", 503)
                sid = secrets.token_urlsafe(32)
                session = {"username": username, "csrf": secrets.token_urlsafe(32), "expires": self.clock() + 300}
                self.sessions[sid] = session
            pending["browser_session"] = sid
            review = html.escape(json.dumps(pending["review"], ensure_ascii=False, indent=2))
            label = {"apply_knowledge_update": "Knowledge 저장·수정", "apply_portfolio_update": "포트폴리오 관측 정보 갱신",
                     "apply_plan_update": "조건부 Plan 생성·교체·종료"}[pending["apply"]]
            response = self._html('<p>' + label + '</p><p>아래 변경 내용과 대상을 확인하세요. 승인 후 Gemini에서 적용해야 저장됩니다.</p>'
                                  '<pre>' + review + '</pre><form method="post"><input type="hidden" name="csrf" value="'
                                  + html.escape(session["csrf"], quote=True) + '"><button name="decision" value="approve">이 변경 승인</button>'
                                  '<button name="decision" value="reject">거절</button></form>')
            response.set_cookie(COOKIE, sid, max_age=300, secure=True, httponly=True, samesite="strict", path="/")
            return response

    def routes(self) -> list[Route]:
        return [Route("/approve/{ticket}", self.browser_review, methods=["GET", "POST"])]
