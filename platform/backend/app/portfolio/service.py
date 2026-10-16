"""Portfolio: the starting position, balances mirrored from the broker, equity, drawdown, benchmark.

Valuation marks BTC at the bid (what a sale would get before fees and slippage), so the equity
shown is a liquidation value, not a mid-price fantasy. The 200 € starting value is measured at the
recorded mid price; the benchmark is "keep the starting BTC", marked the same way as the bot.
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from ..audit import log as audit
from ..core.config import Settings
from ..core.models import Balance, CostEntry, EquitySnapshot, Lot, Order, PortfolioInit
from ..core.numbers import ZERO, floor_to_step
from ..core.timeutil import start_of_day, utcnow
from ..exchange_adapters.base import BalanceInfo
from ..market_data.types import Quote


class PortfolioService:
    def __init__(self, sf: sessionmaker[Session], settings: Settings, account: str, clock=utcnow):
        self.sf = sf
        self.s = settings
        self.account = account
        self.clock = clock

    def get_init(self, session: Session) -> PortfolioInit | None:
        return session.get(PortfolioInit, 1)

    def initialize(self, quote: Quote, seed_broker) -> PortfolioInit | None:
        """Convert 200 € into BTC at the recorded quote, once. Returns None if already done."""
        with self.sf.begin() as session:
            if self.get_init(session) is not None:
                return None
            mid = quote.mid
            base_qty = floor_to_step(self.s.initial_btc_value_eur / mid, self.s.paper_base_step)
            now = self.clock()
            init = PortfolioInit(
                id=1, created_at=now, venue=quote.venue, symbol=quote.symbol, bid=quote.bid, ask=quote.ask, mid=mid,
                base_qty=base_qty, value_eur=base_qty * mid, cash_eur=self.s.initial_cash_eur,
                quote_ts=quote.ts_received, is_fixture=quote.is_fixture,
            )
            session.add(init)
            session.add(Lot(opened_at=now, entry_order_id=None, qty_open=base_qty, entry_price=mid, target_price=None,
                            stop_price=None, expires_at=None, status="open", initial=True))
            audit.record(session, "engine", "portfolio_initialized", None, {
                "quote_venue": quote.venue, "bid": quote.bid, "ask": quote.ask, "mid": mid, "base_qty": base_qty,
                "value_eur_at_mid": base_qty * mid, "cash_eur": self.s.initial_cash_eur, "is_fixture": quote.is_fixture,
            })
        seed_broker({self.s.base_asset: base_qty, self.s.quote_asset: self.s.initial_cash_eur})
        return init

    def mirror_balances(self, balances: dict[str, BalanceInfo]) -> None:
        now = self.clock()
        with self.sf.begin() as session:
            for asset, b in balances.items():
                row = session.get(Balance, (self.account, asset))
                if row is None:
                    session.add(Balance(account=self.account, asset=asset, free=b.free, locked=b.locked, updated_at=now))
                else:
                    row.free, row.locked, row.updated_at = b.free, b.locked, now

    def balances(self, session: Session) -> dict[str, Balance]:
        rows = session.scalars(select(Balance).where(Balance.account == self.account)).all()
        return {r.asset: r for r in rows}

    def holdings(self, session: Session) -> tuple[Decimal, Decimal, Decimal, Decimal]:
        """(cash_free, cash_total, base_free, base_total)."""
        b = self.balances(session)
        q, base = b.get(self.s.quote_asset), b.get(self.s.base_asset)
        return (
            q.free if q else ZERO,
            (q.free + q.locked) if q else ZERO,
            base.free if base else ZERO,
            (base.free + base.locked) if base else ZERO,
        )

    def equity(self, session: Session, bid: Decimal) -> Decimal:
        _, cash_total, _, base_total = self.holdings(session)
        return cash_total + base_total * bid

    def fees_total(self, session: Session) -> Decimal:
        return session.scalar(select(func.coalesce(func.sum(Order.fee_quote), 0))) or ZERO

    def snapshot(self, quote: Quote) -> EquitySnapshot | None:
        now = self.clock()
        with self.sf.begin() as session:
            init = self.get_init(session)
            if init is None:
                return None
            _, cash_total, _, base_total = self.holdings(session)
            equity = cash_total + base_total * quote.bid
            benchmark = init.cash_eur + init.base_qty * quote.bid
            prev_peak = session.scalar(select(func.max(EquitySnapshot.peak_equity_eur))) or ZERO
            peak = max(prev_peak, equity, init.value_eur)
            dd = (peak - equity) / peak * 100 if peak > ZERO else ZERO
            snap = EquitySnapshot(ts=now, cash=cash_total, base_qty=base_total, mark_bid=quote.bid, equity_eur=equity,
                                  benchmark_eur=benchmark, peak_equity_eur=peak, drawdown_pct=dd,
                                  fees_total_eur=self.fees_total(session))
            session.merge(snap)
            return snap

    def day_start_equity(self, session: Session) -> Decimal | None:
        """Equity at the first snapshot of the UTC day (the base for the daily loss limit)."""
        first = session.scalar(
            select(EquitySnapshot).where(EquitySnapshot.ts >= start_of_day(self.clock())).order_by(EquitySnapshot.ts).limit(1)
        )
        if first is not None:
            return first.equity_eur
        last = session.scalar(select(EquitySnapshot).order_by(EquitySnapshot.ts.desc()).limit(1))
        return last.equity_eur if last is not None else None

    def costs_eur(self, session: Session) -> dict[str, Decimal]:
        rows = session.execute(select(CostEntry.category, func.sum(CostEntry.amount_eur)).group_by(CostEntry.category)).all()
        return {cat: total for cat, total in rows}

    def summary(self, session: Session, quote: Quote | None) -> dict:
        init = self.get_init(session)
        cash_free, cash_total, base_free, base_total = self.holdings(session)
        out: dict = {
            "initialized": init is not None,
            "account": self.account,
            "base_asset": self.s.base_asset,
            "quote_asset": self.s.quote_asset,
            "cash_free": cash_free,
            "cash_total": cash_total,
            "base_free": base_free,
            "base_total": base_total,
        }
        max_dd = session.scalar(select(func.max(EquitySnapshot.drawdown_pct))) or ZERO
        fees = self.fees_total(session)
        costs = self.costs_eur(session)
        out["fees_trading_eur"] = fees
        out["costs_eur"] = {k: costs.get(k, ZERO) for k in ("jev", "news", "infra")}
        out["max_drawdown_pct"] = max_dd
        if init is not None:
            out["init"] = {
                "created_at": init.created_at, "venue": init.venue, "bid": init.bid, "ask": init.ask, "mid": init.mid,
                "base_qty": init.base_qty, "value_eur": init.value_eur, "cash_eur": init.cash_eur,
                "is_fixture": init.is_fixture,
            }
        if init is not None and quote is not None:
            equity = cash_total + base_total * quote.bid
            benchmark = init.cash_eur + init.base_qty * quote.bid
            start_value = init.value_eur + init.cash_eur
            analysis_costs = sum(out["costs_eur"].values(), ZERO)
            out.update({
                "mark_bid": quote.bid,
                "mark_source": quote.venue,
                "equity_eur": equity,
                "benchmark_hold_eur": benchmark,
                "pnl_vs_start_eur": equity - start_value,
                "pnl_vs_start_after_analysis_costs_eur": equity - start_value - analysis_costs,
                "pnl_vs_hold_eur": equity - benchmark,
            })
        return out
