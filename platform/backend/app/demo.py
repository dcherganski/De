"""SYNTHETIC demo data for previewing the dashboard and for the browser smoke test.

Everything written here is labelled: venue "fixture:synthetic-demo", is_fixture=True, signals with
config_version "demo" and a reason saying they are not from Jev, news on example.invalid. The seed
refuses to run on a database that already has a portfolio, and the engine refuses to continue a
fixture portfolio on a live feed, so demo numbers cannot mix with a real paper run.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy.orm import Session, sessionmaker

from .core.config import Settings
from .core.models import NewsItem, PortfolioInit, Signal
from .core.timeutil import utcnow
from .market_data.types import Quote, TradeTick

DEMO_VENUE = "fixture:synthetic-demo"
DEMO_REASON = "СИНТЕТИЧЕН ДЕМО СИГНАЛ — не е от Jev"


def synthetic_events(minutes: int = 40, start: datetime | None = None, seed: int = 7, mid: str = "60000",
                     step_s: int = 5, venue: str = "fixture:synthetic") -> list:
    """Deterministic SYNTHETIC random walk: a quote and a trade every `step_s` seconds."""
    rng = random.Random(seed)
    price = Decimal(mid)
    events: list = []
    t = start or utcnow()
    end = t + timedelta(minutes=minutes)
    while t < end:
        price *= Decimal(1) + Decimal(str(round(rng.gauss(0, 0.0006), 6)))
        price = price.quantize(Decimal("0.1"))
        half_spread = Decimal("0.5")
        events.append(Quote(venue, "BTC/EUR", price - half_spread, price + half_spread, t,
                            bid_qty=Decimal("0.8"), ask_qty=Decimal("0.8"), ts_exchange=t, is_fixture=True))
        events.append(TradeTick(venue, "BTC/EUR", price, Decimal("0.01"), rng.choice(["buy", "sell"]),
                                t, t, trade_id=str(len(events)), is_fixture=True))
        t += timedelta(seconds=step_s)
    return events


class DemoStrategy:
    """Scripted stand-in for Jev, used only for the demo. Its signals say so in `reason`."""

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
                         p_close_above=Decimal("0.5"), p_touch=Decimal("0.6"), p_close_below=Decimal("0.3"),
                         valid=True, reason=DEMO_REASON, config_version="demo")
            s.add(sig)
            s.flush()
            return sig.id

    async def periodic(self, engine):
        pass

    def news_trigger(self, engine, items):
        return None


async def seed_demo(sf: sessionmaker[Session], settings: Settings, minutes: int = 90) -> int:
    from .engine.runner import Engine, SimClock
    from .paper_broker.broker import PaperBroker

    with sf() as session:
        if session.get(PortfolioInit, 1) is not None:
            raise SystemExit("Базата вече има портфейл. Демото се зарежда само в празна, отделна база.")
    start = utcnow() - timedelta(minutes=minutes)
    events = synthetic_events(minutes=minutes, start=start, seed=21, venue=DEMO_VENUE)
    clock = SimClock(start)
    s = settings.model_copy(update={"paper_partial_fills": False, "min_seconds_between_trades": 0})

    class _Feed:
        venue = DEMO_VENUE
        is_fixture = True

        async def fetch_candles(self, interval_s=300, since=None):
            return []

    holder: dict = {}
    broker = PaperBroker(sf, s, lambda: holder["e"].market.quote, clock=clock)
    engine = Engine(s, sf, _Feed(), broker, strategy=DemoStrategy(["HOLD", "SELL", "HOLD", "BUY", "HOLD", "BUY"]),
                    clock=clock)
    holder["e"] = engine
    await engine.bootstrap()
    engine.market.feed_connected = True
    for ev in events:
        await engine.handle(ev)
        while not engine.triggers.empty():
            trig = engine.triggers.get_nowait()
            sid = await engine.strategy.evaluate(engine, trig)
            await engine.act_on_signal(sid)
    engine.heartbeat()
    with sf.begin() as session:
        for i, title in enumerate(("DEMO: synthetic headline about bitcoin ETF flows",
                                   "DEMO: synthetic headline about an exchange outage")):
            ts = clock() - timedelta(minutes=20 * (i + 1))
            session.add(NewsItem(provider="fixture", source="synthetic-demo", url=f"https://example.invalid/demo/{i}",
                                 url_norm=f"https://example.invalid/demo/{i}", title=title, summary="Synthetic text.",
                                 title_hash=f"demo{i}", published_at=ts - timedelta(seconds=40), received_at=ts,
                                 latency_s=Decimal(40), relevant=True, is_fixture=True))
    return len(events)
