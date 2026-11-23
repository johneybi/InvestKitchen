import asyncio

from protocol.v1.security.oauth_resource import JWTTokenVerifier
from protocol.v1.tests.test_mcp_http import _config, _gateway, _jwks, _jwt, PERMISSIONS, signing_key
from protocol.v1.transport.mcp_read_surface import RemoteReadSurface, ReadSurfaceError
import pytest


def surface_and_token(key, permissions):
    config = _config(permissions)
    verifier = JWTTokenVerifier(config, _jwks(key))
    token = asyncio.run(verifier.verify_token(_jwt(key)))
    return RemoteReadSurface(_gateway(), config), token


def test_context_requires_all_grants_and_confines_portfolio(signing_key):
    surface, token = surface_and_token(signing_key, PERMISSIONS)
    assert "get_stock_analysis_context" not in surface.tool_names(surface.grant(token))
    with pytest.raises(ReadSurfaceError):
        surface.call_tool(token, "get_stock_analysis_context", {"portfolio_id": "portfolio-alpha"})
    surface, token = surface_and_token(signing_key, [*PERMISSIONS, "policy.read", "plan.read"])
    assert "get_stock_analysis_context" in surface.tool_names(surface.grant(token))
    with pytest.raises(ReadSurfaceError):
        surface.call_tool(token, "get_stock_analysis_context", {"portfolio_id": "portfolio-beta"})
    result = surface.call_tool(token, "get_stock_analysis_context", {"portfolio_id": "portfolio-alpha"})
    assert result["portfolio_id"] == "portfolio-alpha"
    assert result["execution_authorized"] is False
