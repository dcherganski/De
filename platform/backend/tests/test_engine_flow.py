"""End-to-end engine run on SYNTHETIC events (no network, no Jev): the full PAPER loop."""

from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select

from app.core.models import (
    Balance,
    BotState,
    Candle,
    ControlCommand,
    EquitySnapshot,
    Lot,
    Order,
    PortfolioInit,
    RiskDecision,
    Signal,
)
from app.core.numbers import floor_to_step
from app.engine.runner import Engine, SimClock
from app.paper_broker.broker import PaperBroker

from .conftest import T0, synthetic_events


class ListFeed:
    venue = "fixture:synthetic"
    is_fixture = True

    def __init__(self, events):
        self.events = events

    async def fetch_candles(self, interval_s=300, since=None):
        return []


class FakeStrategy:
    """Stands in for Jev: emits the scripted action on each closed candle. Labelled as a test double."""

    jev_ok = None
    last_jev_at = None

    def __init__(self, actions):
        self.actions = list(actions)

    async def evaluate(self, engine, trig):
        action = self.actions.pop(0) if self.actions else "HOLD"
        q = engine.market.quote
        with engine.sf.begin() as s:
            sig = Signal(created_at=engine.clock(), trigger=trig.kind, trigger_ref=trig.ref, venue=q.venue,
                         symbol=q.symbol, p0_bid=q.bid, p0_ask=q.ask, p0_mid=q.mid, target_pct=Decimal("1.0"),
                         stop_pct=Decimal("0.5"), horizon_end=engine.clock() + timedelta(minutes=30), action=action,
                         valid=True, reason="test double", config_version="test")
            s.add(sig)
            s.flush()
            return sig.id

    async def periodic(self, engine):
        pass

    def news_trigger(self, engine, items):
        return None


async def run_engine(sf, settings, events, strategy=None, **overrides):
    clock = SimClock(T0)
    s = settings.model_copy(update={"paper_partial_fills": False, "min_seconds_between_trades": 0, **overrides})
    holder = {}
    broker = PaperBroker(sf, s, lambda: holder["e"].market.quote, clock=clock)
    engine = Engine(s, sf, ListFeed(events), broker, strategy=strategy, clock=clock)
    holder["e"] = engine
    await engine.bootstrap()
    engine.market.feed_connected = True
    for ev in events:
        await engine.handle(ev)
        while not engine.triggers.empty():
            trig = engine.triggers.get_nowait()
            if engine.strategy is not None:
                sid = await engine.strategy.evaluate(engine, trig)
                await engine.act_on_signal(sid)
    return engine


async def test_paper_loop_initializes_200_eur_of_btc_and_builds_candles(sf, settings):
    events = synthetic_events(minutes=16)
    await run_engine(sf, settings, events)
    with sf() as s:
        init = s.get(PortfolioInit, 1)
        first = events[0]
        assert init.mid == first.mid and init.is_fixture
        assert init.base_qty == floor_to_step(Decimal("200") / first.mid, Decimal("0.00000001"))
        assert init.cash_eur == 0
        bals = {b.asset: b for b in s.scalars(select(Balance))}
        assert bals["BTC"].free == init.base_qty and bals["EUR"].free == 0
        assert s.scalar(select(func.count()).select_from(Candle).where(Candle.closed.is_(True))) == 3
        assert s.scalar(select(func.count()).select_from(EquitySnapshot)) >= 10
        assert s.scalar(select(func.count(Order.id))) == 0  # no analyzer, no orders
        (lot,) = s.scalars(select(Lot)).all()
        assert lot.initial and lot.qty_open == init.base_qty


async def test_buy_is_blocked_without_cash_then_sell_then_buy(sf, settings):
    events = synthetic_events(minutes=21)
    await run_engine(sf, settings, events, FakeStrategy(["BUY", "SELL", "BUY", "HOLD"]))
    with sf() as s:
        decisions = s.scalars(select(RiskDecision).order_by(RiskDecision.id)).all()
        assert decisions[0].intent == "BUY" and not decisions[0].allowed
        assert any("0 €" in r for r in decisions[0].reasons)
        assert decisions[1].intent == "SELL" and decisions[1].allowed
        assert decisions[2].intent == "BUY" and decisions[2].allowed
        orders = s.scalars(select(Order).order_by(Order.id)).all()
        assert [(o.intent, o.side) for o in orders[:2]] == [("SELL", "sell"), ("BUY", "buy")]
        assert all(o.intent.startswith("EXIT_") for o in orders[2:])  # the synthetic rally may hit the target
        assert all(o.status == "filled" and o.mode == "paper" for o in orders)
        assert orders[0].qty * orders[0].avg_price <= Decimal("100")  # max order value
        cash = s.get(Balance, ("paper", "EUR"))
        assert cash.free >= 0 and cash.locked == 0


async def test_pause_blocks_new_orders_and_does_not_sell(sf, settings):
    events = synthetic_events(minutes=11)
    clock = SimClock(T0)
    engine = await run_engine(sf, settings, events[:2])
    with sf.begin() as s:
        s.get(BotState, 1).entries_paused = True
    engine.strategy = FakeStrategy(["SELL", "SELL"])
    for ev in events[2:]:
        await engine.handle(ev)
        while not engine.triggers.empty():
            trig = engine.triggers.get_nowait()
            await engine.act_on_signal(await engine.strategy.evaluate(engine, trig))
    del clock
    with sf() as s:
        assert s.scalar(select(func.count(Order.id))) == 0
        rd = s.scalars(select(RiskDecision)).all()
        assert rd and all(not d.allowed for d in rd)
        assert all(any("спрени" in r for r in d.reasons) for d in rd)


async def test_manual_sell_command_goes_through_risk(sf, settings):
    events = synthetic_events(minutes=2)
    engine = await run_engine(sf, settings, events)
    with sf.begin() as s:
        s.add(ControlCommand(created_at=engine.clock(), kind="sell_position", payload={}, requested_by="tester"))
    await engine.process_commands()
    with sf() as s:
        cmd = s.scalar(select(ControlCommand))
        assert cmd.status == "done" and "поръчка" in cmd.result
        order = s.scalar(select(Order))
        assert order.intent == "MANUAL_SELL" and order.status == "filled"


async def test_take_profit_exit_on_quotes_without_jev(sf, settings):
    events = synthetic_events(minutes=11, seed=3)
    engine = await run_engine(sf, settings, events, FakeStrategy(["SELL", "BUY"]),
                              take_profit_pct=Decimal("0.0001"), max_hold_min=600)
    # keep feeding a rising synthetic market; the bot lot should exit on take profit
    from app.market_data.types import Quote
    last = [e for e in events if isinstance(e, Quote)][-1]
    for i in range(1, 30):
        t = last.ts_received + timedelta(seconds=5 * i)
        px = last.bid + Decimal(20 * i)
        await engine.handle(Quote("fixture:synthetic", "BTC/EUR", px, px + 1, t, Decimal(1), Decimal(1), ts_exchange=t,
                                  is_fixture=True))
    with sf() as s:
        intents = [o.intent for o in s.scalars(select(Order).order_by(Order.id))]
        assert "EXIT_TP" in intents
        bot_lots = s.scalars(select(Lot).where(Lot.initial.is_(False))).all()
        assert bot_lots and all(lot.status == "closed" for lot in bot_lots)
