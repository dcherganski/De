"""The one interface every broker implements: paper simulation and real venues alike.

Methods are async; every quantity and price is Decimal. `place_order` must be idempotent on
`client_order_id`: sending the same id twice returns the existing order instead of a new one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Protocol, runtime_checkable

from ..market_data.types import MarketRules, Quote


class OrderStatus(str, Enum):
    NEW = "new"  # written locally, not yet acknowledged by the broker
    OPEN = "open"
    PARTIALLY_FILLED = "partially_filled"
    FILLED = "filled"
    CANCELLED = "cancelled"
    REJECTED = "rejected"
    UNKNOWN = "unknown"  # we sent it and do not know what happened (timeout, crash)
    NOT_FOUND = "not_found"  # the broker has no order with this client id

    @property
    def is_final(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELLED, OrderStatus.REJECTED)


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str
    symbol: str
    side: str  # buy | sell
    qty: Decimal
    order_type: str = "market"


@dataclass
class OrderState:
    client_order_id: str
    status: OrderStatus
    qty: Decimal = Decimal(0)
    filled_qty: Decimal = Decimal(0)
    avg_price: Decimal | None = None
    fee_quote: Decimal = Decimal(0)
    exchange_order_id: str | None = None
    reject_reason: str | None = None


@dataclass(frozen=True)
class FillInfo:
    venue_fill_id: str
    client_order_id: str
    qty: Decimal
    price: Decimal
    fee_quote: Decimal
    ts: datetime


@dataclass(frozen=True)
class BalanceInfo:
    asset: str
    free: Decimal
    locked: Decimal = Decimal(0)

    @property
    def total(self) -> Decimal:
        return self.free + self.locked


@dataclass
class AdapterStatus:
    name: str
    available: bool
    market_data: bool
    trading: bool  # can place orders through this adapter at all
    live_trading_verified: bool  # tested end-to-end against the real venue
    reason: str
    docs: list[str] = field(default_factory=list)
    checked_on: str = ""
    key_permissions: str = ""


class BrokerError(Exception):
    pass


class BrokerTimeout(BrokerError):
    """The outcome is unknown: the order may or may not exist. Check status before resending."""


class BrokerUnavailable(BrokerError):
    pass


@runtime_checkable
class BrokerAdapter(Protocol):
    name: str

    def status(self) -> AdapterStatus: ...

    async def get_balances(self) -> dict[str, BalanceInfo]: ...

    async def get_quotes(self, symbol: str) -> Quote | None: ...

    async def get_market_rules(self, symbol: str) -> MarketRules: ...

    async def place_order(self, req: OrderRequest) -> OrderState: ...

    async def cancel_order(self, client_order_id: str) -> OrderState: ...

    async def get_order_status(self, client_order_id: str) -> OrderState: ...

    async def get_fills(self, client_order_id: str | None = None) -> list[FillInfo]: ...
