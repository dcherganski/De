"""Kraken public market data: WebSocket v2 ticker + trades, REST OHLC backfill.

Checked against the official docs on 2026-10-09:
- WebSocket v2 endpoint wss://ws.kraken.com/v2; `ticker` channel fields bid, bid_qty, ask, ask_qty,
  last, volume, vwap, timestamp, with event_trigger "bbo"
  (docs.kraken.com/exchange/api-reference/spot-websocket-v2/ticker).
- `trade` channel data items: symbol, side, price, qty, ord_type, trade_id, timestamp
  (docs.kraken.com/exchange/api-reference/spot-websocket-v2/trade).
- REST GET https://api.kraken.com/0/public/OHLC, result keyed by pair with
  [time, open, high, low, close, vwap, volume, count] and "last"
  (docs.kraken.com/api-reference/market-data/get-ohlc-data, /exchange/guides/general/historical-data).

No API key is used here. Numbers are parsed as Decimal straight from the JSON text.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
import websockets

from ..core.timeutil import UTC, parse_iso, utcnow
from .types import CandleBar, FeedStatus, Quote, TradeTick

log = logging.getLogger(__name__)

WS_URL = "wss://ws.kraken.com/v2"
REST_URL = "https://api.kraken.com/0/public"
VENUE = "kraken"

# WebSocket v2 uses "BTC/EUR"; REST v0 accepts the legacy "XBTEUR" pair name.
REST_PAIRS = {"BTC/EUR": "XBTEUR", "BTC/USD": "XBTUSD", "ETH/EUR": "ETHEUR"}


def parse_ws_message(raw: str, symbol: str, received: datetime | None = None) -> list[Quote | TradeTick]:
    """Turn one WS v2 frame into events; unknown or unrelated frames yield nothing."""
    msg = json.loads(raw, parse_float=Decimal, parse_int=Decimal)
    received = received or utcnow()
    channel = msg.get("channel")
    out: list[Quote | TradeTick] = []
    if channel == "ticker" and msg.get("type") in ("snapshot", "update"):
        for d in msg.get("data", []):
            if d.get("symbol") != symbol:
                continue
            ts = parse_iso(d["timestamp"]) if d.get("timestamp") else None
            out.append(
                Quote(
                    venue=VENUE,
                    symbol=symbol,
                    bid=Decimal(d["bid"]),
                    ask=Decimal(d["ask"]),
                    bid_qty=Decimal(d["bid_qty"]) if d.get("bid_qty") is not None else None,
                    ask_qty=Decimal(d["ask_qty"]) if d.get("ask_qty") is not None else None,
                    last=Decimal(d["last"]) if d.get("last") is not None else None,
                    ts_exchange=ts,
                    ts_received=received,
                )
            )
    elif channel == "trade" and msg.get("type") in ("snapshot", "update"):
        for d in msg.get("data", []):
            if d.get("symbol") != symbol:
                continue
            out.append(
                TradeTick(
                    venue=VENUE,
                    symbol=symbol,
                    price=Decimal(d["price"]),
                    qty=Decimal(d["qty"]),
                    side=str(d["side"]),
                    ts_exchange=parse_iso(d["timestamp"]),
                    ts_received=received,
                    trade_id=str(d.get("trade_id")),
                )
            )
    return out


def parse_ohlc(payload: dict, symbol: str, interval_s: int, now: datetime | None = None) -> list[CandleBar]:
    if payload.get("error"):
        raise RuntimeError(f"Kraken OHLC error: {payload['error']}")
    result = payload["result"]
    rows = next(v for k, v in result.items() if k != "last")
    now = now or utcnow()
    bars = []
    for row in rows:
        open_time = datetime.fromtimestamp(int(row[0]), UTC)
        bars.append(
            CandleBar(
                venue=VENUE,
                symbol=symbol,
                interval_s=interval_s,
                open_time=open_time,
                open=Decimal(str(row[1])),
                high=Decimal(str(row[2])),
                low=Decimal(str(row[3])),
                close=Decimal(str(row[4])),
                volume=Decimal(str(row[6])),
                trade_count=int(row[7]),
                # The newest row is the interval still in progress.
                closed=open_time + timedelta(seconds=interval_s) <= now,
            )
        )
    return bars


class KrakenFeed:
    venue = VENUE
    is_fixture = False

    def __init__(self, symbol: str, reconnect_max_s: float = 30.0):
        self.symbol = symbol
        self.reconnect_max_s = reconnect_max_s

    async def stream(self) -> AsyncIterator[Quote | TradeTick | FeedStatus]:
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(WS_URL, ping_interval=20, open_timeout=10) as ws:
                    for channel, extra in (("ticker", {"event_trigger": "bbo"}), ("trade", {"snapshot": False})):
                        await ws.send(json.dumps({"method": "subscribe", "params": {"channel": channel, "symbol": [self.symbol], **extra}}))
                    yield FeedStatus(VENUE, True, "connected", utcnow())
                    backoff = 1.0
                    async for raw in ws:
                        for event in parse_ws_message(raw, self.symbol):
                            yield event
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network errors: report, back off, reconnect
                log.warning("Kraken feed disconnected: %s", exc)
                yield FeedStatus(VENUE, False, f"disconnected: {type(exc).__name__}", utcnow())
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.reconnect_max_s)

    async def fetch_candles(self, interval_s: int = 300, since: datetime | None = None) -> list[CandleBar]:
        pair = REST_PAIRS.get(self.symbol, self.symbol.replace("/", ""))
        params: dict[str, str | int] = {"pair": pair, "interval": interval_s // 60}
        if since is not None:
            params["since"] = int(since.timestamp())
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{REST_URL}/OHLC", params=params)
            resp.raise_for_status()
            return parse_ohlc(json.loads(resp.text, parse_float=Decimal), self.symbol, interval_s)
