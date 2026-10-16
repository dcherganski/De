"""Bybit public market data (V5): spot WebSocket order book level 1 + trades, REST klines and rules.

Checked against the official docs (bybit-exchange.github.io/docs/v5) on 2026-10-09:
- Regional REST hosts: EEA accounts use https://api.bybit.eu, global accounts https://api.bybit.com
  (/v5/guide). No dedicated EEA WebSocket host is listed, so public streams use
  wss://stream.bybit.com/v5/public/spot (/v5/ws/connect). Quotes from that stream are labelled as
  Bybit's public market, not as execution prices on a Bybit EU account.
- Heartbeat: send {"op":"ping"} every 20 s (/v5/ws/connect).
- `orderbook.1.{symbol}`: spot level 1 sends snapshots only (10 ms), re-sent after 3 s without change;
  data.b / data.a are [price, size] strings, `ts` is the system time in ms (/v5/websocket/public/orderbook).
- `publicTrade.{symbol}`: data items T (ms), s, S (Buy/Sell taker side), v, p, i (/v5/websocket/public/trade).
- GET /v5/market/kline: list of [startTime, open, high, low, close, volume, turnover] strings,
  newest first; the newest candle may still be open (/v5/market/kline).
- GET /v5/market/orderbook (spot limit default 1): result.b / result.a, ts (/v5/market/orderbook).
- GET /v5/market/instruments-info (spot): lotSizeFilter.basePrecision, minOrderAmt, priceFilter.tickSize;
  minOrderQty is deprecated (/v5/market/instrument).

No API key is used for any of this.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from decimal import Decimal

import httpx
import websockets

from ..core.timeutil import UTC, utcnow
from .types import CandleBar, FeedStatus, MarketRules, Quote, TradeTick

log = logging.getLogger(__name__)

VENUE = "bybit"
REST_URLS = {"eu": "https://api.bybit.eu", "global": "https://api.bybit.com"}
WS_PUBLIC_SPOT = "wss://stream.bybit.com/v5/public/spot"  # no EEA-specific stream is documented
PING_EVERY_S = 20


class BybitError(RuntimeError):
    def __init__(self, code: int, message: str):
        super().__init__(f"Bybit {code}: {message}")
        self.code = code
        self.message = message


def to_bybit_symbol(symbol: str) -> str:
    return symbol.replace("/", "").upper()


def ms_to_dt(value) -> datetime:
    ms = int(value)
    return datetime.fromtimestamp(ms // 1000, UTC) + timedelta(milliseconds=ms % 1000)


def check_ret(payload: dict) -> dict:
    code = int(payload.get("retCode", -1))
    if code != 0:
        raise BybitError(code, str(payload.get("retMsg", "")))
    return payload.get("result") or {}


def parse_ws_message(raw: str | dict, symbol: str, received: datetime | None = None) -> list[Quote | TradeTick]:
    """Turn one public spot frame into events. Acks, pongs and other topics yield nothing."""
    msg = json.loads(raw) if isinstance(raw, str) else raw
    received = received or utcnow()
    topic = msg.get("topic", "")
    bsym = to_bybit_symbol(symbol)
    if topic == f"orderbook.1.{bsym}":
        data = msg.get("data") or {}
        bids, asks = data.get("b") or [], data.get("a") or []
        if not bids or not asks:  # one side empty: there is no two-sided quote to trade on
            return []
        return [Quote(venue=VENUE, symbol=symbol, bid=Decimal(bids[0][0]), ask=Decimal(asks[0][0]),
                      bid_qty=Decimal(bids[0][1]), ask_qty=Decimal(asks[0][1]),
                      ts_exchange=ms_to_dt(msg["ts"]) if msg.get("ts") is not None else None, ts_received=received)]
    if topic == f"publicTrade.{bsym}":
        return [TradeTick(venue=VENUE, symbol=symbol, price=Decimal(d["p"]), qty=Decimal(d["v"]),
                          side=str(d["S"]).lower(), ts_exchange=ms_to_dt(d["T"]), ts_received=received,
                          trade_id=str(d.get("i")))
                for d in msg.get("data") or [] if d.get("s", bsym) == bsym]
    return []


def parse_kline(payload: dict, symbol: str, interval_s: int, now: datetime | None = None) -> list[CandleBar]:
    rows = check_ret(payload).get("list") or []
    now = now or utcnow()
    bars = []
    for row in reversed(rows):  # API order is newest first
        open_time = ms_to_dt(row[0])
        bars.append(CandleBar(venue=VENUE, symbol=symbol, interval_s=interval_s, open_time=open_time,
                              open=Decimal(row[1]), high=Decimal(row[2]), low=Decimal(row[3]), close=Decimal(row[4]),
                              volume=Decimal(row[5]), trade_count=0,
                              closed=open_time + timedelta(seconds=interval_s) <= now))
    return bars


def parse_orderbook(payload: dict, symbol: str, received: datetime | None = None) -> Quote | None:
    res = check_ret(payload)
    bids, asks = res.get("b") or [], res.get("a") or []
    if not bids or not asks:
        return None
    return Quote(venue=VENUE, symbol=symbol, bid=Decimal(bids[0][0]), ask=Decimal(asks[0][0]),
                 bid_qty=Decimal(bids[0][1]), ask_qty=Decimal(asks[0][1]),
                 ts_exchange=ms_to_dt(res["ts"]) if res.get("ts") is not None else None,
                 ts_received=received or utcnow())


def parse_instrument(payload: dict, symbol: str) -> MarketRules:
    items = check_ret(payload).get("list") or []
    bsym = to_bybit_symbol(symbol)
    item = next((i for i in items if i.get("symbol") == bsym), None)
    if item is None:
        raise BybitError(-1, f"{bsym} is not listed for spot trading")
    if item.get("status") not in (None, "Trading"):
        raise BybitError(-1, f"{bsym} status is {item.get('status')}")
    lot = item.get("lotSizeFilter") or {}
    step = Decimal(lot["basePrecision"])
    return MarketRules(symbol=symbol, min_qty=step, qty_step=step, min_cost=Decimal(lot.get("minOrderAmt") or "0"),
                       price_tick=Decimal((item.get("priceFilter") or {}).get("tickSize") or "0") or None,
                       source="Bybit /v5/market/instruments-info",
                       notes=[f"maxMarketOrderQty {lot.get('maxMarketOrderQty')}"] if lot.get("maxMarketOrderQty") else [])


async def public_get(base_url: str, path: str, params: dict, transport: httpx.AsyncBaseTransport | None = None) -> dict:
    async with httpx.AsyncClient(base_url=base_url, timeout=10, transport=transport) as client:
        resp = await client.get(path, params=params)
        resp.raise_for_status()
        return resp.json()


class BybitFeed:
    venue = VENUE
    is_fixture = False

    def __init__(self, symbol: str, region: str = "eu", reconnect_max_s: float = 30.0,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.symbol = symbol
        self.region = region
        self.rest_url = REST_URLS[region]
        self.reconnect_max_s = reconnect_max_s
        self.transport = transport

    @property
    def source_label(self) -> str:
        return "Bybit публичен spot поток (stream.bybit.com)"

    async def _pinger(self, ws) -> None:
        while True:
            await asyncio.sleep(PING_EVERY_S)
            await ws.send(json.dumps({"op": "ping"}))

    async def stream(self) -> AsyncIterator[Quote | TradeTick | FeedStatus]:
        bsym = to_bybit_symbol(self.symbol)
        backoff = 1.0
        while True:
            pinger = None
            try:
                async with websockets.connect(WS_PUBLIC_SPOT, ping_interval=None, open_timeout=10) as ws:
                    await ws.send(json.dumps({"op": "subscribe", "args": [f"orderbook.1.{bsym}", f"publicTrade.{bsym}"]}))
                    pinger = asyncio.create_task(self._pinger(ws))
                    yield FeedStatus(VENUE, True, "connected", utcnow())
                    backoff = 1.0
                    async for raw in ws:
                        msg = json.loads(raw)
                        if msg.get("op") == "subscribe" and msg.get("success") is False:
                            raise BybitError(-1, f"subscription refused: {msg.get('ret_msg')}")
                        for event in parse_ws_message(msg, self.symbol):
                            yield event
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # network errors: report, back off, reconnect
                log.warning("Bybit feed disconnected: %s", exc)
                yield FeedStatus(VENUE, False, f"disconnected: {type(exc).__name__}", utcnow())
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, self.reconnect_max_s)
            finally:
                if pinger is not None:
                    pinger.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await pinger

    async def fetch_candles(self, interval_s: int = 300, since: datetime | None = None) -> list[CandleBar]:
        params: dict[str, str | int] = {"category": "spot", "symbol": to_bybit_symbol(self.symbol),
                                        "interval": str(interval_s // 60), "limit": 200}
        if since is not None:
            params["start"] = int(since.timestamp() * 1000)
        payload = await public_get(self.rest_url, "/v5/market/kline", params, self.transport)
        return parse_kline(payload, self.symbol, interval_s)

    async def fetch_rules(self) -> MarketRules:
        payload = await public_get(self.rest_url, "/v5/market/instruments-info",
                                   {"category": "spot", "symbol": to_bybit_symbol(self.symbol)}, self.transport)
        return parse_instrument(payload, self.symbol)
