"""Replays a recorded or synthetic file as if it were a feed.

Every event it emits carries is_fixture=True and the venue name "fixture:<file>", so test data can
never be shown or stored as live prices. File format: one JSON object per line, either
{"type": "quote", "t": ISO, "bid": "...", "ask": "...", "bid_qty": "...", "ask_qty": "..."} or
{"type": "trade", "t": ISO, "price": "...", "qty": "...", "side": "buy"}.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from ..core.timeutil import parse_iso, utcnow
from .types import CandleBar, FeedStatus, Quote, TradeTick


def load_fixture_events(path: str | Path, symbol: str) -> list[Quote | TradeTick]:
    venue = f"fixture:{Path(path).name}"
    events: list[Quote | TradeTick] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        d = json.loads(line, parse_float=Decimal)
        t = parse_iso(d["t"])
        if d["type"] == "quote":
            events.append(
                Quote(
                    venue=venue,
                    symbol=symbol,
                    bid=Decimal(str(d["bid"])),
                    ask=Decimal(str(d["ask"])),
                    bid_qty=Decimal(str(d["bid_qty"])) if "bid_qty" in d else None,
                    ask_qty=Decimal(str(d["ask_qty"])) if "ask_qty" in d else None,
                    ts_exchange=t,
                    ts_received=t,
                    is_fixture=True,
                )
            )
        elif d["type"] == "trade":
            events.append(
                TradeTick(
                    venue=venue,
                    symbol=symbol,
                    price=Decimal(str(d["price"])),
                    qty=Decimal(str(d["qty"])),
                    side=d.get("side", "buy"),
                    ts_exchange=t,
                    ts_received=t,
                    trade_id=d.get("id"),
                    is_fixture=True,
                )
            )
    return events


class FixtureFeed:
    is_fixture = True

    def __init__(self, path: str | Path, symbol: str, realtime: bool = False, speed: float = 1.0,
                 rebase_to_now: bool = False):
        self.path = Path(path)
        self.symbol = symbol
        self.venue = f"fixture:{self.path.name}"
        self.realtime = realtime
        self.speed = speed
        self.rebase_to_now = rebase_to_now  # shift timestamps so a demo replay looks current (still labelled)

    async def stream(self) -> AsyncIterator[Quote | TradeTick | FeedStatus]:
        events = load_fixture_events(self.path, self.symbol)
        yield FeedStatus(self.venue, True, "replaying fixture (not live data)", utcnow())
        offset = (utcnow() - events[0].ts_exchange) if (self.rebase_to_now and events) else None
        prev: datetime | None = None
        for ev in events:
            if self.realtime and prev is not None:
                await asyncio.sleep(max(0.0, (ev.ts_exchange - prev).total_seconds() / self.speed))
            prev = ev.ts_exchange
            if offset is not None:
                ev = replace(ev, ts_exchange=ev.ts_exchange + offset, ts_received=utcnow())
            yield ev
        yield FeedStatus(self.venue, False, "fixture finished", utcnow())

    async def fetch_candles(self, interval_s: int = 300, since: datetime | None = None) -> list[CandleBar]:
        return []
