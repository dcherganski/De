"""Test fixtures. Tests run against a real PostgreSQL database (TEST_DATABASE_URL).

All market data here is SYNTHETIC or copied from official documentation examples, and is labelled
as such (venue "fixture:..." / is_fixture=True). None of it is live data.
"""

from __future__ import annotations

import os
import random
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.core.config import Settings
from app.core.db import get_engine, session_factory
from app.core.timeutil import UTC
from app.market_data.types import Quote, TradeTick

BACKEND = Path(__file__).resolve().parents[1]
TEST_DB = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+psycopg://trader:trader_local_only@127.0.0.1:5432/trading_test"
)
T0 = datetime(2026, 1, 5, 12, 0, tzinfo=UTC)  # synthetic clock start, not a real market time


@pytest.fixture(scope="session")
def migrated_db():
    cfg = Config(str(BACKEND / "alembic.ini"))
    cfg.set_main_option("script_location", str(BACKEND / "alembic"))
    cfg.attributes["database_url"] = TEST_DB
    engine = get_engine(TEST_DB)
    with engine.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    command.upgrade(cfg, "head")
    return TEST_DB


@pytest.fixture
def sf(migrated_db):
    engine = get_engine(migrated_db)
    with engine.begin() as conn:
        tables = [r[0] for r in conn.execute(text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' AND tablename <> 'alembic_version'"))]
        conn.execute(text("ALTER TABLE audit_log DISABLE TRIGGER USER"))
        conn.execute(text("TRUNCATE " + ", ".join(tables) + " RESTART IDENTITY CASCADE"))
        conn.execute(text("ALTER TABLE audit_log ENABLE TRIGGER USER"))
    return session_factory(migrated_db)


@pytest.fixture
def settings(migrated_db) -> Settings:
    return Settings(
        _env_file=None,
        database_url=migrated_db,
        quote_venue="fixture",
        news_provider="none",
        typesafe_api_key=None,
        secrets_master_key=None,
    )


class QuoteBox:
    """Mutable holder the paper broker reads its quotes from."""

    def __init__(self, quote: Quote | None = None):
        self.quote = quote

    def __call__(self) -> Quote | None:
        return self.quote


class Clock:
    def __init__(self, now: datetime = T0):
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kw) -> None:
        self.now += timedelta(**kw)


def make_quote(bid: str, ask: str, ts: datetime = T0, bid_qty: str | None = "1", ask_qty: str | None = "1",
               venue: str = "fixture:test") -> Quote:
    return Quote(venue=venue, symbol="BTC/EUR", bid=Decimal(bid), ask=Decimal(ask),
                 bid_qty=Decimal(bid_qty) if bid_qty else None, ask_qty=Decimal(ask_qty) if ask_qty else None,
                 ts_exchange=ts, ts_received=ts, is_fixture=True)


def synthetic_events(minutes: int = 40, start: datetime = T0, seed: int = 7, mid: str = "60000",
                     step_s: int = 5) -> list:
    """Deterministic SYNTHETIC random walk: a quote and a trade every `step_s` seconds."""
    rng = random.Random(seed)
    price = Decimal(mid)
    events: list = []
    t = start
    end = start + timedelta(minutes=minutes)
    while t < end:
        price *= Decimal(1) + Decimal(str(round(rng.gauss(0, 0.0006), 6)))
        price = price.quantize(Decimal("0.1"))
        half_spread = Decimal("0.5")
        events.append(Quote("fixture:synthetic", "BTC/EUR", price - half_spread, price + half_spread, t,
                            bid_qty=Decimal("0.8"), ask_qty=Decimal("0.8"), ts_exchange=t, is_fixture=True))
        events.append(TradeTick("fixture:synthetic", "BTC/EUR", price, Decimal("0.01"), rng.choice(["buy", "sell"]),
                                t, t, trade_id=str(len(events)), is_fixture=True))
        t += timedelta(seconds=step_s)
    return events
