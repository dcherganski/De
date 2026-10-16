"""Builds fixed-interval candles from trades and reports when one closes.

A candle closes when the first trade (or clock tick) of the next interval arrives. Only closed
candles drive the strategy; the open one is shown but never treated as final.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from ..core.timeutil import ensure_utc, floor_time
from .types import CandleBar, TradeTick


class CandleAggregator:
    def __init__(self, venue: str, symbol: str, interval_s: int = 300, is_fixture: bool = False):
        self.venue = venue
        self.symbol = symbol
        self.interval_s = interval_s
        self.is_fixture = is_fixture
        self.current: CandleBar | None = None
        self._on_close: list[Callable[[CandleBar], None]] = []

    def on_close(self, callback: Callable[[CandleBar], None]) -> None:
        self._on_close.append(callback)

    def seed(self, candle: CandleBar) -> None:
        """Continue an open candle loaded from the database or REST backfill after a restart."""
        if not candle.closed:
            self.current = candle

    def _bucket(self, ts: datetime) -> datetime:
        return floor_time(ts, self.interval_s)

    def _close_current(self) -> CandleBar | None:
        if self.current is None:
            return None
        closed = self.current
        closed.closed = True
        self.current = None
        for cb in self._on_close:
            cb(closed)
        return closed

    def add_trade(self, trade: TradeTick) -> CandleBar | None:
        """Returns the candle that this trade closed, if any."""
        bucket = self._bucket(ensure_utc(trade.ts_exchange))
        closed = None
        if self.current is not None and bucket > self.current.open_time:
            closed = self._close_current()
        if self.current is not None and bucket < self.current.open_time:
            return None  # late trade for an already closed interval: ignored, never rewrites history
        if self.current is None:
            self.current = CandleBar(
                venue=self.venue,
                symbol=self.symbol,
                interval_s=self.interval_s,
                open_time=bucket,
                open=trade.price,
                high=trade.price,
                low=trade.price,
                close=trade.price,
                is_fixture=self.is_fixture,
            )
        c = self.current
        c.high = max(c.high, trade.price)
        c.low = min(c.low, trade.price)
        c.close = trade.price
        c.volume += trade.qty
        c.trade_count += 1
        return closed

    def tick(self, now: datetime) -> CandleBar | None:
        """Close the open candle on the clock even when no trade arrives (quiet markets)."""
        if self.current is None:
            return None
        if ensure_utc(now) >= self.current.open_time + timedelta(seconds=self.interval_s):
            return self._close_current()
        return None
