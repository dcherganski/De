"""Test fixtures. Tests run against a real PostgreSQL database (TEST_DATABASE_URL).

All market data here is SYNTHETIC or copied from official documentation examples, and is labelled
as such (venue "fixture:..." / is_fixture=True). None of it is live data.
"""

from __future__ import annotations

import os
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
from app.demo import synthetic_events as _synthetic_events
from app.market_data.types import Quote

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
    """Deterministic SYNTHETIC random walk (see app.demo), starting at the synthetic clock T0."""
    return _synthetic_events(minutes=minutes, start=start, seed=seed, mid=mid, step_s=step_s)
