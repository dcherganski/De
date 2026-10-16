"""Read-only MCP interface on SYNTHETIC engine data: auth required, tools read, nothing writes."""

import json

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select
from starlette.testclient import TestClient

from app.core.models import AuditEntry, Order
from app.mcp.server import build_server, create_app

from .conftest import T0, synthetic_events
from .test_engine_flow import FakeStrategy, run_engine

TOKEN = "test-mcp-token-not-a-real-secret-0123456789"
HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}


def rpc(method, params=None, id_=1):
    return {"jsonrpc": "2.0", "id": id_, "method": method, "params": params or {}}


def test_refuses_to_start_without_strong_token(sf, settings):
    with pytest.raises(SystemExit):
        create_app(settings, sf)
    with pytest.raises(SystemExit):
        create_app(settings.model_copy(update={"mcp_token": SecretStr("short")}), sf)


async def test_tools_are_read_only_and_answer(sf, settings):
    await run_engine(sf, settings, synthetic_events(minutes=21), FakeStrategy(["BUY", "SELL", "BUY"]))
    server = build_server(sf, settings, clock=lambda: T0)
    tools = {t.name: t for t in await server.list_tools()}
    assert set(tools) == {"portfolio", "bot_status", "recent_signals", "trade_history", "simulation_results"}
    assert all(t.annotations.read_only_hint and not t.annotations.destructive_hint for t in tools.values())
    with sf() as s:
        orders_before = s.scalar(select(func.count(Order.id)))
        audit_before = s.scalar(select(func.count(AuditEntry.id)))
    texts = {}
    for name in tools:
        res = await server.call_tool(name, {})
        assert not res.is_error
        texts[name] = json.loads(res.content[0].text)
    assert texts["bot_status"]["mode"] == "PAPER" and texts["bot_status"]["is_test_data"]
    assert texts["portfolio"]["init"]["is_fixture"] and "ТЕСТОВИ ДАННИ" in texts["portfolio"]["quote"]["note"]
    assert texts["trade_history"] and texts["recent_signals"]["signals"]
    assert "шум" in texts["simulation_results"]["calibration"]["note"]
    with sf() as s:  # reading changed nothing
        assert s.scalar(select(func.count(Order.id))) == orders_before
        assert s.scalar(select(func.count(AuditEntry.id))) == audit_before


def test_http_requires_bearer_token(sf, settings):
    app = create_app(settings.model_copy(update={"mcp_token": SecretStr(TOKEN)}), sf)
    with TestClient(app, base_url="http://localhost") as client:
        assert client.post("/mcp", json=rpc("tools/list"), headers=HEADERS).status_code == 401
        bad = HEADERS | {"Authorization": "Bearer wrong-token"}
        assert client.post("/mcp", json=rpc("tools/list"), headers=bad).status_code == 401
        good = HEADERS | {"Authorization": f"Bearer {TOKEN}"}
        init = client.post("/mcp", headers=good, json=rpc("initialize", {
            "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}))
        assert init.status_code == 200, init.text
        listed = client.post("/mcp", headers=good | {"MCP-Protocol-Version": "2025-06-18"}, json=rpc("tools/list", id_=2))
        assert listed.status_code == 200, listed.text
        assert "trade_history" in listed.text and TOKEN not in listed.text
