"""Simulated exchange that fills market orders against live quotes from the selected feed.

Assumptions (also shown on the dashboard):
- Buys fill at the ask plus slippage, sells at the bid minus slippage. The quote may come from a
  different venue than the one you would really trade on; it is labelled with its source.
- A taker fee is charged in the quote currency on every fill.
- Orders must respect a minimum size, a quantity step and a minimum notional.
- With partial fills on, one quote fills at most `participation` × the displayed top-of-book size;
  the rest waits for the next quote. Deeper book levels are not modelled.
- No stop orders are simulated: protective exits are market orders sent by the engine once the
  bid crosses the level, so the exit price can be worse than the level (gaps, slippage).
- Funds for a buy are reserved at placement; if the price rises past the reserve, the unfillable
  remainder is cancelled rather than overdrawing.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from ..core.config import Settings
from ..core.models import PaperBalance, PaperFill, PaperOrder
from ..core.numbers import BPS, ZERO, floor_to_step
from ..core.timeutil import age_s, utcnow
from ..exchange_adapters.base import (
    AdapterStatus,
    BalanceInfo,
    FillInfo,
    OrderRequest,
    OrderState,
    OrderStatus,
)
from ..market_data.types import MarketRules, Quote

OPEN_STATES = ("open", "partially_filled")


class PaperBroker:
    name = "paper"

    def __init__(self, sf: sessionmaker[Session], settings: Settings, quote_source: Callable[[], Quote | None],
                 clock=utcnow):
        self.sf = sf
        self.s = settings
        self.quote_source = quote_source
        self.clock = clock
        self.fee_rate = settings.paper_fee_rate
        self.slip = settings.paper_slippage_bps * BPS

    # ------------------------------------------------------------------ info
    def status(self) -> AdapterStatus:
        return AdapterStatus(
            name="paper",
            available=True,
            market_data=False,
            trading=True,
            live_trading_verified=False,
            reason="Симулация с живи котировки от избрания източник; не изпраща поръчки никъде.",
        )

    def assumptions(self) -> dict:
        return {
            "fee_rate": str(self.fee_rate),
            "slippage_bps": str(self.s.paper_slippage_bps),
            "min_qty": str(self.s.paper_min_order_base),
            "qty_step": str(self.s.paper_base_step),
            "min_cost": str(self.s.paper_min_cost_quote),
            "partial_fills": self.s.paper_partial_fills,
            "top_of_book_participation": str(self.s.paper_top_of_book_participation),
            "notes": [
                "покупка по ask + приплъзване, продажба по bid − приплъзване",
                "дълбочината на книгата не се моделира",
                "защитните изходи са пазарни поръчки; цената може да е по-лоша от нивото",
            ],
        }

    def rules(self, symbol: str) -> MarketRules:
        return MarketRules(
            symbol=symbol,
            min_qty=self.s.paper_min_order_base,
            qty_step=self.s.paper_base_step,
            min_cost=self.s.paper_min_cost_quote,
            source="config (симулация)",
        )

    async def get_market_rules(self, symbol: str) -> MarketRules:
        return self.rules(symbol)

    async def get_quotes(self, symbol: str) -> Quote | None:
        return self.quote_source()

    async def get_balances(self) -> dict[str, BalanceInfo]:
        with self.sf() as session:
            rows = session.scalars(select(PaperBalance)).all()
            return {r.asset: BalanceInfo(r.asset, r.free, r.locked) for r in rows}

    def init_balances(self, balances: dict[str, Decimal]) -> bool:
        """Seed the simulated account once; returns False if it already exists."""
        with self.sf.begin() as session:
            if session.scalar(select(PaperBalance).limit(1)) is not None:
                return False
            now = self.clock()
            for asset, amount in balances.items():
                session.add(PaperBalance(asset=asset, free=amount, locked=ZERO, updated_at=now))
            return True

    # ------------------------------------------------------------------ helpers
    def _bal(self, session: Session, asset: str) -> PaperBalance:
        bal = session.get(PaperBalance, asset, with_for_update=True)
        if bal is None:
            bal = PaperBalance(asset=asset, free=ZERO, locked=ZERO, updated_at=self.clock())
            session.add(bal)
            session.flush()
        return bal

    @staticmethod
    def _state(o: PaperOrder) -> OrderState:
        return OrderState(
            client_order_id=o.client_order_id,
            status=OrderStatus(o.status),
            qty=o.qty,
            filled_qty=o.filled_qty,
            avg_price=o.avg_price,
            fee_quote=o.fee_quote,
            exchange_order_id=f"paper-{o.id}",
            reject_reason=o.reject_reason,
        )

    def _fresh_quote(self) -> Quote | None:
        q = self.quote_source()
        if q is None or not q.is_sane():
            return None
        a = age_s(q.ts_received, self.clock())
        if a is not None and a > self.s.max_quote_age_s:
            return None
        return q

    # ------------------------------------------------------------------ orders
    async def place_order(self, req: OrderRequest) -> OrderState:
        base, quote_ccy = req.symbol.split("/")
        with self.sf.begin() as session:
            existing = session.scalar(select(PaperOrder).where(PaperOrder.client_order_id == req.client_order_id))
            if existing is not None:
                return self._state(existing)  # idempotent resend
            now = self.clock()
            order = PaperOrder(client_order_id=req.client_order_id, symbol=req.symbol, side=req.side, qty=req.qty,
                               status="open", created_at=now, updated_at=now)
            reason = self._validate(req)
            q = self._fresh_quote()
            if reason is None and q is None:
                reason = "няма свежа котировка"
            if reason is None:
                price_est = q.ask * (1 + self.slip) if req.side == "buy" else q.bid * (1 - self.slip)
                if req.qty * price_est < self.s.paper_min_cost_quote:
                    reason = f"стойността е под минимума {self.s.paper_min_cost_quote} {quote_ccy}"
            if reason is None:
                if req.side == "buy":
                    reserve = req.qty * price_est * (1 + self.fee_rate)
                    bal = self._bal(session, quote_ccy)
                    if bal.free < reserve:
                        reason = f"недостатъчни средства: нужни {reserve:.2f}, налични {bal.free:.2f} {quote_ccy}"
                    else:
                        bal.free -= reserve
                        bal.locked += reserve
                        bal.updated_at = now
                        order.locked_quote = reserve
                else:
                    bal = self._bal(session, base)
                    if bal.free < req.qty:
                        reason = f"недостатъчно {base}: нужни {req.qty}, налични {bal.free}"
                    else:
                        bal.free -= req.qty
                        bal.locked += req.qty
                        bal.updated_at = now
            if reason is not None:
                order.status = "rejected"
                order.reject_reason = reason
                session.add(order)
                session.flush()
                return self._state(order)
            session.add(order)
            session.flush()
            self._match(session, order, q)
            return self._state(order)

    def _validate(self, req: OrderRequest) -> str | None:
        if req.side not in ("buy", "sell"):
            return f"непозната страна {req.side}"
        if req.order_type != "market":
            return "симулацията поддържа само пазарни поръчки"
        if req.qty <= ZERO:
            return "количеството трябва да е положително"
        if req.qty < self.s.paper_min_order_base:
            return f"количеството е под минимума {self.s.paper_min_order_base}"
        if floor_to_step(req.qty, self.s.paper_base_step) != req.qty:
            return f"количеството не е кратно на стъпката {self.s.paper_base_step}"
        return None

    def on_quote(self, quote: Quote) -> list[str]:
        """Match resting orders against a new quote; returns the client ids that changed."""
        if not quote.is_sane():
            return []
        changed = []
        with self.sf.begin() as session:
            orders = session.scalars(
                select(PaperOrder).where(PaperOrder.status.in_(OPEN_STATES)).order_by(PaperOrder.id).with_for_update()
            ).all()
            for order in orders:
                if self._match(session, order, quote):
                    changed.append(order.client_order_id)
        return changed

    def _match(self, session: Session, order: PaperOrder, q: Quote) -> bool:
        base, quote_ccy = order.symbol.split("/")
        remaining = order.qty - order.filled_qty
        if remaining <= ZERO or order.status not in OPEN_STATES:
            return False
        step = self.s.paper_base_step
        fill_qty = remaining
        top = q.ask_qty if order.side == "buy" else q.bid_qty
        if self.s.paper_partial_fills and top is not None:
            fill_qty = min(remaining, floor_to_step(top * self.s.paper_top_of_book_participation, step))
        if fill_qty <= ZERO:
            return False
        now = self.clock()
        if order.side == "buy":
            price = q.ask * (1 + self.slip)
            affordable = floor_to_step(order.locked_quote / (price * (1 + self.fee_rate)), step)
            if affordable < fill_qty:
                fill_qty = affordable
            if fill_qty <= ZERO:
                self._finish(session, order, "cancelled", "цената се е качила над блокираните средства; остатъкът е отменен")
                return True
            fee = fill_qty * price * self.fee_rate
            cost = fill_qty * price + fee
            qbal, bbal = self._bal(session, quote_ccy), self._bal(session, base)
            order.locked_quote -= cost
            qbal.locked -= cost
            bbal.free += fill_qty
            qbal.updated_at = bbal.updated_at = now
        else:
            price = q.bid * (1 - self.slip)
            fee = fill_qty * price * self.fee_rate
            proceeds = fill_qty * price - fee
            qbal, bbal = self._bal(session, quote_ccy), self._bal(session, base)
            bbal.locked -= fill_qty
            qbal.free += proceeds
            qbal.updated_at = bbal.updated_at = now
        prev_notional = (order.avg_price or ZERO) * order.filled_qty
        order.filled_qty += fill_qty
        order.avg_price = (prev_notional + price * fill_qty) / order.filled_qty
        order.fee_quote += fee
        order.updated_at = now
        session.add(PaperFill(paper_order_id=order.id, qty=fill_qty, price=price, fee_quote=fee, ts=now,
                              quote_bid=q.bid, quote_ask=q.ask))
        if order.filled_qty >= order.qty:
            self._finish(session, order, "filled")
        else:
            order.status = "partially_filled"
            if order.side == "buy" and order.locked_quote <= ZERO:
                self._finish(session, order, "cancelled", "блокираните средства са изчерпани")
        return True

    def _finish(self, session: Session, order: PaperOrder, status: str, reason: str | None = None) -> None:
        """Close an order and release whatever it still had reserved."""
        base, quote_ccy = order.symbol.split("/")
        now = self.clock()
        if order.side == "buy" and order.locked_quote > ZERO:
            qbal = self._bal(session, quote_ccy)
            qbal.locked -= order.locked_quote
            qbal.free += order.locked_quote
            qbal.updated_at = now
            order.locked_quote = ZERO
        if order.side == "sell" and status != "filled":
            left = order.qty - order.filled_qty
            if left > ZERO:
                bbal = self._bal(session, base)
                bbal.locked -= left
                bbal.free += left
                bbal.updated_at = now
        order.status = status
        order.reject_reason = reason or order.reject_reason
        order.updated_at = now

    async def cancel_order(self, client_order_id: str) -> OrderState:
        with self.sf.begin() as session:
            order = session.scalar(
                select(PaperOrder).where(PaperOrder.client_order_id == client_order_id).with_for_update()
            )
            if order is None:
                return OrderState(client_order_id, OrderStatus.NOT_FOUND)
            if order.status in OPEN_STATES:
                self._finish(session, order, "cancelled", "отменена по искане")
            return self._state(order)

    async def get_order_status(self, client_order_id: str) -> OrderState:
        with self.sf() as session:
            order = session.scalar(select(PaperOrder).where(PaperOrder.client_order_id == client_order_id))
            if order is None:
                return OrderState(client_order_id, OrderStatus.NOT_FOUND)
            return self._state(order)

    async def get_fills(self, client_order_id: str | None = None) -> list[FillInfo]:
        with self.sf() as session:
            stmt = select(PaperFill, PaperOrder.client_order_id).join(PaperOrder, PaperOrder.id == PaperFill.paper_order_id)
            if client_order_id is not None:
                stmt = stmt.where(PaperOrder.client_order_id == client_order_id)
            rows = session.execute(stmt.order_by(PaperFill.id)).all()
            return [FillInfo(f"paper-fill-{f.id}", cid, f.qty, f.price, f.fee_quote, f.ts) for f, cid in rows]
