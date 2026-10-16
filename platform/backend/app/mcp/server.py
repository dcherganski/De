"""Optional read-only MCP interface: `uvicorn --factory app.mcp.server:create_app --port 8100`.

Tools only read what the engine already wrote: portfolio, bot status, signals, trade history and
simulation results. Nothing here can place, cancel or change anything. Every request needs
`Authorization: Bearer <MCP_TOKEN>`; the server refuses to start without a strong token. Exchange
keys and secrets are never part of any answer.
"""

from __future__ import annotations

import hmac
import json

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations
from sqlalchemy import select
from starlette.responses import JSONResponse

from ..api.read import latest_quote, live_block_reasons, order_view, quote_view, risk_view, signal_view
from ..core.config import Settings, get_settings
from ..core.db import session_factory
from ..core.jsonutil import jsonable
from ..core.models import BotState, EngineStatus, Order, RiskDecision, Signal
from ..core.secrets import configure_logging
from ..core.timeutil import age_s, utcnow
from ..portfolio.service import PortfolioService
from ..strategy.outcomes import calibration

READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)


def _dump(data) -> str:
    return json.dumps(jsonable(data), ensure_ascii=False)


def build_server(sf, s: Settings, clock=utcnow) -> MCPServer:
    server = MCPServer(
        name="trading-platform-readonly",
        instructions=("Read-only view of a personal crypto PAPER-trading platform. Values are decimal strings in EUR "
                      "unless stated. Probabilities are model estimates, not proven accuracy. No tool can trade."),
    )

    @server.tool(annotations=READ_ONLY)
    def portfolio() -> str:
        """Balances, value in EUR at the bid, P&L after analysis costs, comparison with holding the starting BTC."""
        with sf() as session:
            q = latest_quote(session, s)
            out = PortfolioService(sf, s, account="paper", clock=clock).summary(session, q)
        out["quote"] = quote_view(q, s, clock())
        return _dump(out)

    @server.tool(annotations=READ_ONLY)
    def bot_status() -> str:
        """Mode (PAPER/LIVE), whether new orders are paused, engine and data health, why LIVE is off."""
        t = clock()
        with sf() as session:
            st = session.get(EngineStatus, 1)
            bot = session.get(BotState, 1)
        return _dump({
            "mode": s.mode.upper(), "live_blocked_because": live_block_reasons(s),
            "entries_paused": bool(bot and bot.entries_paused), "venue": bot.selected_venue if bot else s.quote_venue,
            "symbol": bot.selected_symbol if bot else s.symbol,
            "engine_heartbeat_age_s": age_s(st.heartbeat_at, t) if st else None,
            "data_ok": st.data_ok if st else None, "data_block_reason": st.data_block_reason if st else None,
            "is_test_data": st.is_fixture if st else None,
        })

    @server.tool(annotations=READ_ONLY)
    def recent_signals(limit: int = 20) -> str:
        """Latest Jev signals with probabilities, target and the recorded 30-minute outcome."""
        limit = max(1, min(int(limit), 200))
        t = clock()
        with sf() as session:
            rows = session.scalars(select(Signal).order_by(Signal.id.desc()).limit(limit)).all()
            blocks = session.scalars(select(RiskDecision).where(RiskDecision.allowed.is_(False))
                                     .order_by(RiskDecision.id.desc()).limit(5)).all()
        return _dump({"signals": [signal_view(r, t) for r in rows], "recent_blocks": [risk_view(b) for b in blocks]})

    @server.tool(annotations=READ_ONLY)
    def trade_history(limit: int = 20) -> str:
        """Orders (paper unless stated) with status, filled quantity, average price and fees."""
        limit = max(1, min(int(limit), 200))
        with sf() as session:
            rows = session.scalars(select(Order).order_by(Order.id.desc()).limit(limit)).all()
        return _dump([order_view(o) for o in rows])

    @server.tool(annotations=READ_ONLY)
    def simulation_results() -> str:
        """Forecast calibration (Brier vs. base rate) plus portfolio result vs. holding. Small samples are noise."""
        with sf() as session:
            q = latest_quote(session, s)
            pf = PortfolioService(sf, s, account="paper", clock=clock).summary(session, q)
        keys = ("equity_eur", "benchmark_hold_eur", "pnl_vs_start_after_analysis_costs_eur", "pnl_vs_hold_eur",
                "max_drawdown_pct", "fees_trading_eur", "costs_eur")
        return _dump({"calibration": calibration(sf), "portfolio": {k: pf.get(k) for k in keys}})

    return server


class BearerAuth:
    """ASGI guard: every HTTP request must carry the configured bearer token (constant-time compare)."""

    def __init__(self, app, token: str):
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers") or [])
            if not hmac.compare_digest(headers.get(b"authorization", b""), self.expected):
                await JSONResponse({"error": "unauthorized"}, status_code=401,
                                   headers={"WWW-Authenticate": "Bearer"})(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_app(settings: Settings | None = None, sf=None, clock=utcnow):
    if settings is None:
        configure_logging()
    s = settings or get_settings()
    token = s.mcp_token.get_secret_value() if s.mcp_token else ""
    if len(token) < 32:
        raise SystemExit("MCP_TOKEN is missing or shorter than 32 characters; run `python -m app.cli gen-keys`.")
    server = build_server(sf or session_factory(s.database_url), s, clock)
    return BearerAuth(server.streamable_http_app(stateless_http=True, json_response=True, host="0.0.0.0"), token)
