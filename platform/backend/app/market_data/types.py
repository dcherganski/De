"""Market data value objects. Prices and sizes are Decimal; timestamps are UTC."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from ..core.numbers import ZERO


@dataclass(frozen=True)
class Quote:
    venue: str
    symbol: str
    bid: Decimal
    ask: Decimal
    ts_received: datetime
    bid_qty: Decimal | None = None
    ask_qty: Decimal | None = None
    last: Decimal | None = None
    ts_exchange: datetime | None = None
    is_fixture: bool = False

    @property
    def mid(self) -> Decimal:
        return (self.bid + self.ask) / 2

    @property
    def spread(self) -> Decimal:
        return self.ask - self.bid

    @property
    def spread_pct(self) -> Decimal:
        return self.spread / self.mid * 100 if self.mid > ZERO else Decimal("Infinity")

    def is_sane(self) -> bool:
        return self.bid > ZERO and self.ask > ZERO and self.ask >= self.bid


@dataclass(frozen=True)
class TradeTick:
    venue: str
    symbol: str
    price: Decimal
    qty: Decimal
    side: str  # taker side: buy | sell
    ts_exchange: datetime
    ts_received: datetime
    trade_id: str | None = None
    is_fixture: bool = False


@dataclass
class CandleBar:
    venue: str
    symbol: str
    interval_s: int
    open_time: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal = ZERO
    trade_count: int = 0
    closed: bool = False
    is_fixture: bool = False


@dataclass(frozen=True)
class FeedStatus:
    venue: str
    connected: bool
    detail: str = ""
    ts: datetime | None = None


@dataclass
class MarketRules:
    """Exchange limits that an order must respect, with where they came from."""

    symbol: str
    min_qty: Decimal
    qty_step: Decimal
    min_cost: Decimal
    price_tick: Decimal | None = None
    source: str = "config"
    notes: list[str] = field(default_factory=list)
