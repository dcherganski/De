"""The trading engine process: market data in, decisions out.

Analysis and execution are separate paths. Jev is asked only on a closed 5-minute candle or a
significant news item (rate limited). Price ticks never wait for Jev: they only feed the paper
broker and the exit checks (take profit, stop, time limit), which run on the latest quote.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, sessionmaker

from ..audit import log as audit
from ..audit.costs import accrue_fixed_costs
from ..core.config import Settings
from ..core.models import (
    BotState,
    Candle,
    ControlCommand,
    EngineStatus,
    Lot,
    QuoteSnapshot,
    RiskDecision,
    Signal,
)
from ..core.timeutil import age_s, utcnow
from ..exchange_adapters.base import BrokerAdapter
from ..execution.service import DuplicateOrder, ExecutionService
from ..market_data.candles import CandleAggregator
from ..market_data.state import MarketState
from ..market_data.types import CandleBar, FeedStatus, Quote, TradeTick
from ..portfolio.service import PortfolioService
from ..risk.engine import MANUAL_SELL, REDUCE, RiskContext, RiskEngine, RiskResult

log = logging.getLogger(__name__)


class SimClock:
    """Clock that follows event timestamps, so fixture replays are deterministic and never 'stale'."""

    def __init__(self, start: datetime | None = None):
        self.now = start or utcnow()

    def advance(self, ts: datetime) -> None:
        if ts > self.now:
            self.now = ts

    def __call__(self) -> datetime:
        return self.now


class EngineRestart(Exception):
    """The dashboard picked another exchange; the process exits and Docker starts it on the new feed."""


@dataclass
class Trigger:
    kind: str  # candle_close | news
    ref: str
    at: datetime
    candle: CandleBar | None = None
    news_ids: tuple[int, ...] = ()


class Engine:
    def __init__(self, settings: Settings, sf: sessionmaker[Session], feed, broker: BrokerAdapter,
                 strategy=None, news=None, clock=None):
        self.s = settings
        self.sf = sf
        self.feed = feed
        self.broker = broker
        self.strategy = strategy
        self.news = news
        self.sim_clock = clock if isinstance(clock, SimClock) else None
        self.clock = clock or utcnow
        self.market = MarketState(venue=feed.venue, symbol=settings.symbol, is_fixture=feed.is_fixture)
        self.agg = CandleAggregator(feed.venue, settings.symbol, 300, is_fixture=feed.is_fixture)
        self.portfolio = PortfolioService(sf, settings, account=broker.name, clock=self.clock)
        self.execution = ExecutionService(sf, broker, settings, self.portfolio, clock=self.clock)
        self.risk = RiskEngine(settings)
        self.triggers: asyncio.Queue[Trigger] = asyncio.Queue()
        self._last_quote_store: datetime | None = None
        self._last_exit_check = 0.0
        self._last_snapshot: datetime | None = None
        self._last_minute_job = 0.0
        self.last_error: str | None = None
        self.rules = None

    # ------------------------------------------------------------------ bootstrap
    def ensure_rows(self) -> None:
        now = self.clock()
        with self.sf.begin() as session:
            if session.get(BotState, 1) is None:
                session.add(BotState(id=1, entries_paused=False, selected_venue=self.feed.venue,
                                     selected_symbol=self.s.symbol, updated_at=now, updated_by="engine"))
            status = session.get(EngineStatus, 1)
            if status is None:
                status = EngineStatus(id=1)
                session.add(status)
            status.started_at = now
            status.feed_venue = self.feed.venue
            status.is_fixture = self.feed.is_fixture
            audit.record(session, "engine", "engine_started", None, {
                "mode": self.s.mode, "feed": self.feed.venue, "broker": self.broker.name, "fixture": self.feed.is_fixture,
                "jev_configured": self.s.typesafe_api_key is not None, "news_provider": self.s.news_provider,
            })

    async def bootstrap(self) -> None:
        self.ensure_rows()
        self.rules = await self.broker.get_market_rules(self.s.symbol)
        n = await self.execution.reconcile()
        if n:
            log.info("reconciled %d orders after restart", n)
        self._load_last_candle()
        try:
            bars = await self.feed.fetch_candles(300)
            for bar in bars:
                self._store_candle(bar)
            closed = [b for b in bars if b.closed]
            if closed:
                self.market.last_closed_candle = closed[-1]
            open_bars = [b for b in bars if not b.closed]
            if open_bars:
                self.agg.seed(open_bars[-1])
        except Exception as exc:  # backfill is best effort; live candles still form from trades
            log.warning("candle backfill failed: %s", exc)

    def _load_last_candle(self) -> None:
        with self.sf() as session:
            row = session.scalar(
                select(Candle).where(Candle.venue == self.feed.venue, Candle.symbol == self.s.symbol,
                                     Candle.interval_s == 300, Candle.closed.is_(True))
                .order_by(Candle.open_time.desc()).limit(1)
            )
            if row is not None:
                self.market.last_closed_candle = CandleBar(row.venue, row.symbol, row.interval_s, row.open_time,
                                                           row.open, row.high, row.low, row.close, row.volume,
                                                           row.trade_count, True, row.is_fixture)

    # ------------------------------------------------------------------ events
    async def handle(self, event) -> None:
        if self.sim_clock is not None:
            ts = getattr(event, "ts_received", None) or getattr(event, "ts", None)
            if ts is not None:
                self.sim_clock.advance(ts)
        if isinstance(event, FeedStatus):
            self.market.feed_connected = event.connected
            self.market.feed_detail = event.detail
            with self.sf.begin() as session:
                audit.record(session, "engine", "feed_status", self.feed.venue,
                             {"connected": event.connected, "detail": event.detail})
            return
        self.market.update(event)
        if isinstance(event, Quote):
            await self._on_quote(event)
        elif isinstance(event, TradeTick):
            closed = self.agg.add_trade(event)
            if closed is not None:
                await self._on_candle_close(closed)
        closed = self.agg.tick(self.clock())
        if closed is not None:
            await self._on_candle_close(closed)

    async def _on_quote(self, q: Quote) -> None:
        if not q.is_sane():
            return
        with self.sf() as session:
            initialized = self.portfolio.get_init(session) is not None
        if not initialized:
            self.portfolio.initialize(q, self._seed_broker)
            self.portfolio.mirror_balances(await self.broker.get_balances())
        if self._last_quote_store is None or (q.ts_received - self._last_quote_store).total_seconds() >= self.s.quote_snapshot_every_s:
            self._last_quote_store = q.ts_received
            with self.sf.begin() as session:
                session.add(QuoteSnapshot(venue=q.venue, symbol=q.symbol, bid=q.bid, ask=q.ask, bid_qty=q.bid_qty,
                                          ask_qty=q.ask_qty, last=q.last, ts_exchange=q.ts_exchange,
                                          ts_received=q.ts_received, is_fixture=q.is_fixture))
                if q.ts_exchange is not None:
                    audit.latency(session, "quote_receive", (q.ts_received - q.ts_exchange).total_seconds() * 1000)
        on_quote = getattr(self.broker, "on_quote", None)
        if on_quote is not None:
            for cid in on_quote(q):
                await self.execution.sync(cid)
        await self.check_exits()
        if self._last_snapshot is None or (self.clock() - self._last_snapshot).total_seconds() >= 60:
            self._last_snapshot = self.clock()
            self.portfolio.snapshot(q)

    def _seed_broker(self, balances: dict[str, Decimal]) -> None:
        init = getattr(self.broker, "init_balances", None)
        if init is not None:
            init(balances)

    def _store_candle(self, bar: CandleBar) -> None:
        values = dict(venue=bar.venue, symbol=bar.symbol, interval_s=bar.interval_s, open_time=bar.open_time,
                      open=bar.open, high=bar.high, low=bar.low, close=bar.close, volume=bar.volume,
                      trade_count=bar.trade_count, closed=bar.closed, is_fixture=bar.is_fixture)
        stmt = pg_insert(Candle).values(**values)
        stmt = stmt.on_conflict_do_update(index_elements=["venue", "symbol", "interval_s", "open_time"],
                                          set_={k: stmt.excluded[k] for k in values if k not in
                                                ("venue", "symbol", "interval_s", "open_time")})
        with self.sf.begin() as session:
            session.execute(stmt)

    async def _on_candle_close(self, bar: CandleBar) -> None:
        self._store_candle(bar)
        self.market.last_closed_candle = bar
        await self.triggers.put(Trigger("candle_close", bar.open_time.isoformat(), self.clock(), candle=bar))

    # ------------------------------------------------------------------ risk context
    def risk_context(self, session: Session, signal: Signal | None = None, require_candle: bool = True) -> RiskContext:
        """Entries need fresh candles (the analysis used them); exits only need a fresh quote."""
        now = self.clock()
        check = self.market.check(self.s.max_quote_age_s, self.s.max_candle_gap_s, now, require_candle=require_candle)
        cash_free, _, base_free, base_total = self.portfolio.holdings(session)
        state = session.get(BotState, 1)
        q = self.market.quote
        return RiskContext(
            now=now, quote=q, data_ok=check.ok, data_reasons=check.reasons, cash_free=cash_free, base_free=base_free,
            base_total=base_total, open_orders=len(self.execution.active_orders(session)),
            trades_today=self.execution.trades_today(session), last_trade_at=self.execution.last_trade_at(session),
            day_start_equity=self.portfolio.day_start_equity(session),
            equity_now=self.portfolio.equity(session, q.bid) if q else None,
            entries_paused=bool(state and state.entries_paused), rules=self.broker_rules(),
            signal_age_s=age_s(signal.created_at, now) if signal else None,
            signal_valid=signal.valid if signal else True,
        )

    def broker_rules(self):
        if self.rules is None:
            rules = getattr(self.broker, "rules", None)
            self.rules = rules(self.s.symbol) if rules else None
        return self.rules

    def _record_risk(self, session: Session, res: RiskResult, ctx: RiskContext, signal_id: int | None) -> RiskDecision:
        rd = RiskDecision(created_at=ctx.now, signal_id=signal_id, intent=res.intent, allowed=res.allowed, qty=res.qty,
                          reasons=res.reasons + [f"предупреждение: {w}" for w in res.warnings],
                          context={"cash_free": str(ctx.cash_free), "base_free": str(ctx.base_free),
                                   "bid": str(ctx.quote.bid) if ctx.quote else None,
                                   "ask": str(ctx.quote.ask) if ctx.quote else None,
                                   "spread_pct": str(ctx.quote.spread_pct) if ctx.quote else None,
                                   "trades_today": ctx.trades_today, "paused": ctx.entries_paused,
                                   "notional_est": str(res.notional_est)})
        session.add(rd)
        session.flush()
        audit.record(session, "risk", "risk_decision", str(rd.id),
                     {"intent": res.intent, "allowed": res.allowed, "qty": res.qty, "reasons": rd.reasons,
                      "signal_id": signal_id})
        return rd

    # ------------------------------------------------------------------ decisions
    async def act_on_signal(self, signal_id: int) -> RiskDecision | None:
        with self.sf.begin() as session:
            sig = session.get(Signal, signal_id)
            if sig is None or sig.action not in ("BUY", "SELL") or not sig.valid:
                return None
            ctx = self.risk_context(session, sig)
            if sig.action == "BUY":
                res = self.risk.evaluate_buy(ctx)
                side = "buy"
            else:
                res = self.risk.evaluate_sell(ctx, REDUCE, ctx.base_free)
                side = "sell"
            rd = self._record_risk(session, res, ctx, signal_id)
        if res.allowed:
            try:
                await self.execution.submit(side=side, qty=res.qty, intent=res.intent, signal_id=signal_id,
                                            risk_decision_id=rd.id)
            except DuplicateOrder as exc:
                with self.sf.begin() as session:
                    audit.record(session, "engine", "order_skipped", str(signal_id), {"reason": str(exc)})
        return rd

    async def check_exits(self) -> None:
        """Take profit / stop / time exits for lots the bot opened. The starting BTC has none."""
        q = self.market.quote
        if q is None:
            return
        with self.sf() as session:
            if self.execution.active_orders(session):
                return
            lots = list(session.scalars(select(Lot).where(Lot.status == "open", Lot.initial.is_(False))))
        now = self.clock()
        for lot in lots:
            intent = None
            if lot.target_price is not None and q.bid >= lot.target_price:
                intent = "EXIT_TP"
            elif lot.stop_price is not None and q.bid <= lot.stop_price:
                intent = "EXIT_SL"
            elif lot.expires_at is not None and now >= lot.expires_at:
                intent = "EXIT_TIME"
            if intent is None:
                continue
            with self.sf.begin() as session:
                ctx = self.risk_context(session, require_candle=False)
                res = self.risk.evaluate_sell(ctx, intent, lot.qty_open)
                rd = self._record_risk(session, res, ctx, None)
            if res.allowed:
                try:
                    await self.execution.submit(side="sell", qty=res.qty, intent=intent, risk_decision_id=rd.id,
                                                lot_id=lot.id)
                except DuplicateOrder:
                    pass
            return  # one exit per check; the next quote handles the rest

    async def process_commands(self) -> None:
        with self.sf() as session:
            cmds = list(session.scalars(select(ControlCommand).where(ControlCommand.status == "pending")
                                        .order_by(ControlCommand.id)))
        for cmd in cmds:
            result = ""
            try:
                if cmd.kind == "cancel_pending":
                    done = await self.execution.cancel_pending(cmd.requested_by)
                    result = f"отменени: {len(done)}"
                elif cmd.kind == "sell_position":
                    with self.sf.begin() as session:
                        ctx = self.risk_context(session, require_candle=False)
                        res = self.risk.evaluate_sell(ctx, MANUAL_SELL, ctx.base_free)
                        rd = self._record_risk(session, res, ctx, None)
                    if res.allowed:
                        order = await self.execution.submit(side="sell", qty=res.qty, intent=MANUAL_SELL,
                                                            risk_decision_id=rd.id, actor=cmd.requested_by)
                        result = f"поръчка {order.client_order_id}: {order.status}, изпълнени {order.filled_qty}"
                    else:
                        result = "блокирано: " + "; ".join(res.reasons)
                else:
                    result = f"непозната команда {cmd.kind}"
                status = "done"
            except Exception as exc:
                status, result = "failed", f"{type(exc).__name__}: {exc}"
            with self.sf.begin() as session:
                row = session.get(ControlCommand, cmd.id)
                row.status, row.result, row.processed_at = status, result, self.clock()
                audit.record(session, cmd.requested_by, "control_processed", str(cmd.id),
                             {"kind": cmd.kind, "status": status, "result": result})

    def heartbeat(self) -> None:
        now = self.clock()
        check = self.market.check(self.s.max_quote_age_s, self.s.max_candle_gap_s, now)
        with self.sf.begin() as session:
            st = session.get(EngineStatus, 1)
            if st is None:
                return
            st.heartbeat_at = now
            st.feed_connected = self.market.feed_connected
            st.last_quote_at = self.market.quote.ts_received if self.market.quote else None
            st.last_trade_at = self.market.last_trade.ts_received if self.market.last_trade else None
            st.last_candle_close_at = (self.market.last_closed_candle.open_time if self.market.last_closed_candle
                                       else None)
            st.data_ok = check.ok
            st.data_block_reason = "; ".join(check.reasons) or None
            st.last_error = self.last_error
            if self.strategy is not None:
                st.jev_ok = self.strategy.jev_ok
                st.last_jev_at = self.strategy.last_jev_at
            if self.news is not None:
                st.news_ok = self.news.ok
                st.last_news_poll_at = self.news.last_poll_at

    def check_selection(self) -> None:
        """Restart onto the exchange chosen in the dashboard. Fixture replays never switch."""
        if self.feed.is_fixture:
            return
        with self.sf() as session:
            bot = session.get(BotState, 1)
            wanted = (bot.selected_venue, bot.selected_symbol) if bot else (self.feed.venue, self.s.symbol)
        if wanted != (self.feed.venue, self.s.symbol):
            with self.sf.begin() as session:
                audit.record(session, "engine", "engine_restart_for_venue", None,
                             {"from": [self.feed.venue, self.s.symbol], "to": list(wanted), "funds_moved": False})
            raise EngineRestart(f"switching feed to {wanted[0]} {wanted[1]}")

    # ------------------------------------------------------------------ loops
    async def _feed_loop(self) -> None:
        async for event in self.feed.stream():
            try:
                await self.handle(event)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                log.exception("event handling failed")

    async def _strategy_loop(self) -> None:
        while True:
            trig = await self.triggers.get()
            if self.strategy is None:
                continue
            try:
                t0 = time.perf_counter()
                signal_id = await self.strategy.evaluate(self, trig)
                with self.sf.begin() as session:
                    audit.latency(session, "analysis", (time.perf_counter() - t0) * 1000, trig.ref)
                if signal_id is not None:
                    await self.act_on_signal(signal_id)
            except Exception as exc:
                self.last_error = f"strategy: {type(exc).__name__}: {exc}"
                log.exception("strategy evaluation failed")

    async def _timer_loop(self) -> None:
        while True:
            await asyncio.sleep(1)
            try:
                closed = self.agg.tick(self.clock())
                if closed is not None:
                    await self._on_candle_close(closed)
                await self.process_commands()
                self.heartbeat()
                if self.strategy is not None:
                    await self.strategy.periodic(self)
                if time.monotonic() - self._last_minute_job >= 60:
                    self._last_minute_job = time.monotonic()
                    accrue_fixed_costs(self.sf, self.s, self.clock())
                self.check_selection()
            except EngineRestart:
                raise
            except Exception as exc:
                self.last_error = f"timer: {type(exc).__name__}: {exc}"
                log.exception("timer loop failed")

    async def _news_loop(self) -> None:
        while True:
            try:
                new_items = await self.news.poll(self)
                if self.strategy is not None and new_items:
                    trig = self.strategy.news_trigger(self, new_items)
                    if trig is not None:
                        await self.triggers.put(trig)
            except Exception as exc:
                self.last_error = f"news: {type(exc).__name__}: {exc}"
                log.exception("news loop failed")
            await asyncio.sleep(self.s.news_poll_s)

    async def run(self) -> None:
        await self.bootstrap()
        tasks = [self._feed_loop(), self._strategy_loop(), self._timer_loop()]
        if self.news is not None:
            tasks.append(self._news_loop())
        await asyncio.gather(*tasks)
