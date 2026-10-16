"""Order lifecycle: local record first, then the broker, then reconciliation from the broker's view.

Rules that keep money safe:
- Every order gets a client_order_id (UUID) stored with a UNIQUE index before it is sent.
- A second automatic order is refused while another one is not final (local duplicate guard).
- If sending times out or the process dies mid-flight, the order is UNKNOWN. We ask the broker for
  its status first and only resend (with the same client id) when the broker has never seen it.
- Balances and fills are taken from the broker, not computed locally.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ..audit import log as audit
from ..core.config import Settings
from ..core.models import Fill, Lot, Order, Signal
from ..core.numbers import ZERO
from ..core.timeutil import start_of_day, timedelta, utcnow
from ..exchange_adapters.base import (
    BrokerAdapter,
    BrokerError,
    BrokerTimeout,
    OrderRequest,
    OrderState,
    OrderStatus,
)
from ..portfolio.service import PortfolioService

log = logging.getLogger(__name__)

ACTIVE = ("new", "open", "partially_filled", "unknown")


class DuplicateOrder(RuntimeError):
    pass


class ExecutionService:
    def __init__(self, sf: sessionmaker[Session], broker: BrokerAdapter, settings: Settings,
                 portfolio: PortfolioService, clock=utcnow, submit_timeout_s: float = 10.0):
        self.sf = sf
        self.broker = broker
        self.s = settings
        self.portfolio = portfolio
        self.clock = clock
        self.submit_timeout_s = submit_timeout_s
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ queries
    def active_orders(self, session: Session) -> list[Order]:
        return list(session.scalars(select(Order).where(Order.status.in_(ACTIVE)).order_by(Order.id)))

    def trades_today(self, session: Session) -> int:
        return session.scalar(
            select(func.count(Order.id)).where(Order.created_at >= start_of_day(self.clock()),
                                               Order.filled_qty > 0, Order.intent.in_(("BUY", "SELL")))
        ) or 0

    def last_trade_at(self, session: Session):
        return session.scalar(select(func.max(Order.created_at)).where(Order.filled_qty > 0,
                                                                      Order.intent.in_(("BUY", "SELL"))))

    # ------------------------------------------------------------------ submit
    async def submit(self, *, side: str, qty: Decimal, intent: str, signal_id: int | None = None,
                     risk_decision_id: int | None = None, lot_id: int | None = None, actor: str = "engine") -> Order:
        async with self._lock:
            now = self.clock()
            with self.sf.begin() as session:
                if self.active_orders(session):
                    raise DuplicateOrder("друга поръчка още не е приключила")
                order = Order(client_order_id=str(uuid.uuid4()), venue=self.broker.name, mode=self.s.mode,
                              symbol=self.s.symbol, side=side, order_type="market", qty=qty, status="new",
                              intent=intent, signal_id=signal_id, risk_decision_id=risk_decision_id, lot_id=lot_id,
                              created_at=now, updated_at=now)
                session.add(order)
                session.flush()
                audit.record(session, actor, "order_created", order.client_order_id,
                             {"side": side, "qty": qty, "intent": intent, "venue": self.broker.name, "mode": self.s.mode,
                              "signal_id": signal_id, "lot_id": lot_id})
            req = OrderRequest(order.client_order_id, self.s.symbol, side, qty)
            t0 = time.perf_counter()
            self._mark(order.id, submitted_at=self.clock())
            try:
                state = await asyncio.wait_for(self.broker.place_order(req), self.submit_timeout_s)
            except (asyncio.TimeoutError, BrokerTimeout, BrokerError, OSError) as exc:
                log.warning("order %s outcome unknown: %s", order.client_order_id, exc)
                self._apply_state(order.id, OrderState(order.client_order_id, OrderStatus.UNKNOWN),
                                  error=f"{type(exc).__name__}: {exc}")
                state = await self._resolve_unknown(order.client_order_id)
            ack_ms = (time.perf_counter() - t0) * 1000
            with self.sf.begin() as session:
                audit.latency(session, "submit_to_ack", ack_ms, order.client_order_id)
            self._apply_state(order.id, state)
            await self.sync(order.client_order_id)
            with self.sf() as session:
                return session.get(Order, order.id)

    async def _resolve_unknown(self, client_order_id: str) -> OrderState:
        """Status first; resend with the same id only if the broker never received it."""
        try:
            st = await asyncio.wait_for(self.broker.get_order_status(client_order_id), self.submit_timeout_s)
        except (asyncio.TimeoutError, BrokerError, OSError) as exc:
            return OrderState(client_order_id, OrderStatus.UNKNOWN, reject_reason=f"статусът е неизвестен: {exc}")
        if st.status != OrderStatus.NOT_FOUND:
            with self.sf.begin() as session:
                audit.record(session, "engine", "order_status_recovered", client_order_id, {"status": st.status.value})
            return st
        with self.sf.begin() as session:
            order = session.scalar(select(Order).where(Order.client_order_id == client_order_id))
            audit.record(session, "engine", "order_resubmitted", client_order_id,
                         {"why": "брокерът не познава поръчката след неясен резултат"})
            req = OrderRequest(client_order_id, order.symbol, order.side, order.qty)
        try:
            return await asyncio.wait_for(self.broker.place_order(req), self.submit_timeout_s)
        except (asyncio.TimeoutError, BrokerError, OSError) as exc:
            return OrderState(client_order_id, OrderStatus.UNKNOWN, reject_reason=f"повторното изпращане е неясно: {exc}")

    def _mark(self, order_id: int, **fields) -> None:
        with self.sf.begin() as session:
            order = session.get(Order, order_id)
            for k, v in fields.items():
                setattr(order, k, v)
            order.updated_at = self.clock()

    def _apply_state(self, order_id: int, st: OrderState, error: str | None = None) -> None:
        now = self.clock()
        with self.sf.begin() as session:
            order = session.get(Order, order_id, with_for_update=True)
            before = order.status
            order.status = st.status.value if st.status != OrderStatus.NOT_FOUND else "unknown"
            if st.exchange_order_id:
                order.exchange_order_id = st.exchange_order_id
            if st.status not in (OrderStatus.UNKNOWN, OrderStatus.NOT_FOUND):
                order.filled_qty = st.filled_qty
                order.avg_price = st.avg_price
                order.fee_quote = st.fee_quote
                order.ack_at = order.ack_at or now
            if st.reject_reason:
                order.error = st.reject_reason
            if error:
                order.error = error
            if st.status.is_final and order.done_at is None:
                order.done_at = now
            order.updated_at = now
            if before != order.status:
                audit.record(session, "engine", "order_status", order.client_order_id,
                             {"from": before, "to": order.status, "filled_qty": order.filled_qty,
                              "avg_price": order.avg_price, "fee": order.fee_quote, "reason": order.error})

    # ------------------------------------------------------------------ sync
    async def sync(self, client_order_id: str) -> None:
        """Pull status and fills from the broker, record new fills, update lots and balances."""
        st = await self.broker.get_order_status(client_order_id)
        fills = await self.broker.get_fills(client_order_id)
        now = self.clock()
        with self.sf.begin() as session:
            order = session.scalar(select(Order).where(Order.client_order_id == client_order_id).with_for_update())
            if order is None:
                return
            known = set(session.scalars(select(Fill.venue_fill_id).where(Fill.order_id == order.id)))
            new_qty = ZERO
            for f in fills:
                if f.venue_fill_id in known:
                    continue
                session.add(Fill(order_id=order.id, venue=self.broker.name, venue_fill_id=f.venue_fill_id, qty=f.qty,
                                 price=f.price, fee_quote=f.fee_quote, ts=f.ts))
                new_qty += f.qty
                audit.record(session, "engine", "fill", order.client_order_id,
                             {"qty": f.qty, "price": f.price, "fee": f.fee_quote, "fill_id": f.venue_fill_id})
                if order.submitted_at is not None:
                    audit.latency(session, "submit_to_fill", (f.ts - order.submitted_at).total_seconds() * 1000,
                                  order.client_order_id)
            if new_qty > ZERO:
                self._apply_lots(session, order, new_qty, st, now)
        if st.status != OrderStatus.NOT_FOUND:
            with self.sf() as session:
                oid = session.scalar(select(Order.id).where(Order.client_order_id == client_order_id))
            self._apply_state(oid, st)
        self.portfolio.mirror_balances(await self.broker.get_balances())

    def _apply_lots(self, session: Session, order: Order, new_qty: Decimal, st: OrderState, now) -> None:
        if order.side == "buy":
            sig = session.get(Signal, order.signal_id) if order.signal_id else None
            target_pct = sig.target_pct if sig else self.s.target_margin_pct
            stop_pct = sig.stop_pct if sig else self.s.stop_pct
            tp_pct = self.s.take_profit_pct if self.s.take_profit_pct is not None else target_pct
            lot = session.get(Lot, order.lot_id) if order.lot_id else None
            if lot is None:
                lot = Lot(opened_at=now, entry_order_id=order.id, qty_open=ZERO, entry_price=ZERO,
                          expires_at=now + timedelta(minutes=self.s.max_hold_min), status="open", initial=False)
                session.add(lot)
                session.flush()
                order.lot_id = lot.id
            lot.qty_open += new_qty
            # Levels follow the volume-weighted entry as partial fills arrive.
            lot.entry_price = st.avg_price or lot.entry_price
            lot.target_price = lot.entry_price * (1 + tp_pct / 100)
            lot.stop_price = lot.entry_price * (1 - stop_pct / 100)
            return
        remaining = new_qty
        lots = []
        if order.lot_id:
            lot = session.get(Lot, order.lot_id)
            if lot is not None and lot.status == "open":
                lots.append(lot)
        lots += [lot for lot in session.scalars(select(Lot).where(Lot.status == "open").order_by(Lot.opened_at))
                 if lot not in lots]
        for lot in lots:
            if remaining <= ZERO:
                break
            take = min(lot.qty_open, remaining)
            lot.qty_open -= take
            remaining -= take
            if lot.qty_open <= ZERO:
                lot.status = "closed"
                lot.closed_at = now
                lot.exit_reason = order.intent

    async def reconcile(self) -> int:
        """After a restart: settle every order that was not final, asking the broker first."""
        with self.sf() as session:
            pending = [(o.id, o.client_order_id, o.status) for o in self.active_orders(session)]
        for oid, cid, status in pending:
            if status in ("new", "unknown"):
                st = await self._resolve_unknown(cid) if status == "unknown" else await self.broker.get_order_status(cid)
                if st.status == OrderStatus.NOT_FOUND and status == "new":
                    # Written locally but never sent: the broker has no trace, so it is safe to drop.
                    st = OrderState(cid, OrderStatus.CANCELLED, reject_reason="не е изпратена преди рестарта")
                self._apply_state(oid, st)
            await self.sync(cid)
            with self.sf.begin() as session:
                audit.record(session, "engine", "order_reconciled", cid, {"previous_status": status})
        return len(pending)

    async def cancel_pending(self, actor: str) -> list[str]:
        with self.sf() as session:
            pending = [o.client_order_id for o in self.active_orders(session)]
        done = []
        for cid in pending:
            st = await self.broker.cancel_order(cid)
            with self.sf.begin() as session:
                audit.record(session, actor, "order_cancel_requested", cid, {"result": st.status.value})
            await self.sync(cid)
            done.append(cid)
        return done
