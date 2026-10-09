"""Entry point of the engine process: `python -m app.engine.main`.

Builds the feed, broker, news provider and strategy from the environment. PAPER is the default; the
engine refuses to start in LIVE mode unless every guard is satisfied.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from ..audit import log as audit
from ..core.config import Settings, get_settings
from ..core.db import session_factory
from ..core.models import BotState
from ..core.secrets import configure_logging
from ..market_data.bybit import BybitFeed
from ..market_data.fixture import FixtureFeed
from ..market_data.kraken import KrakenFeed
from ..paper_broker.broker import PaperBroker
from .runner import Engine, EngineRestart

log = logging.getLogger("engine")


def build_feed(s: Settings):
    if s.quote_venue == "bybit":
        return BybitFeed(s.symbol, region=s.bybit_region)
    if s.quote_venue == "kraken":
        return KrakenFeed(s.symbol)
    if s.quote_venue == "fixture":
        if not s.fixture_path:
            raise SystemExit("QUOTE_VENUE=fixture needs FIXTURE_PATH")
        return FixtureFeed(s.fixture_path, s.symbol, realtime=True, rebase_to_now=True)
    raise SystemExit(f"unknown QUOTE_VENUE {s.quote_venue}")


def build_strategy(s: Settings, sf):
    try:
        from ..strategy.runner import StrategyRunner
    except ImportError:  # stage 1 build without the analyzer
        return None
    return StrategyRunner.from_settings(s, sf)


def build_news(s: Settings, sf):
    if s.news_provider == "none":
        return None
    from ..news.service import NewsService
    return NewsService.from_settings(s, sf)


def apply_dashboard_selection(s: Settings, sf) -> Settings:
    """The exchange picked in the dashboard wins over QUOTE_VENUE, except for fixture replays."""
    if s.quote_venue == "fixture":
        return s
    with sf() as session:
        bot = session.get(BotState, 1)
    if bot is None or bot.selected_venue not in ("bybit", "kraken"):
        return s
    return s.model_copy(update={"quote_venue": bot.selected_venue, "symbol": bot.selected_symbol})


async def apply_exchange_rules(s: Settings, sf, feed) -> Settings:
    """Simulate with the exchange's own minimum size, step and minimum value when it publishes them."""
    fetch = getattr(feed, "fetch_rules", None)
    if fetch is None:
        return s
    try:
        rules = await fetch()
    except Exception as exc:  # best effort: keep the configured simulation rules
        log.warning("could not load %s trading rules, keeping configured ones: %s", feed.venue, exc)
        return s
    with sf.begin() as session:
        audit.record(session, "engine", "paper_rules_from_exchange", feed.venue,
                     {"min_qty": rules.min_qty, "qty_step": rules.qty_step, "min_cost": rules.min_cost,
                      "source": rules.source})
    return s.model_copy(update={"paper_min_order_base": rules.min_qty, "paper_base_step": rules.qty_step,
                                "paper_min_cost_quote": rules.min_cost})


def check_live_guards(s: Settings) -> None:
    if s.mode != "live":
        return
    reasons = []
    if not s.allow_live_trading:
        reasons.append("ALLOW_LIVE_TRADING is not true")
    reasons.append("no exchange adapter has verified live trading in this build")
    raise SystemExit("LIVE mode refused: " + "; ".join(reasons))


async def amain() -> None:
    configure_logging()
    s = get_settings()
    check_live_guards(s)
    sf = session_factory(s.database_url)
    s = apply_dashboard_selection(s, sf)
    feed = build_feed(s)
    s = await apply_exchange_rules(s, sf, feed)
    holder: dict = {}
    broker = PaperBroker(sf, s, lambda: holder["engine"].market.quote)
    engine = Engine(s, sf, feed, broker, strategy=build_strategy(s, sf), news=build_news(s, sf))
    holder["engine"] = engine
    log.info("engine starting: mode=%s feed=%s broker=%s", s.mode, feed.venue, broker.name)
    await engine.run()


def main() -> None:
    try:
        asyncio.run(amain())
    except KeyboardInterrupt:
        sys.exit(0)
    except EngineRestart as exc:
        log.info("restarting: %s", exc)
        sys.exit(3)  # compose restarts the service (restart: unless-stopped)


if __name__ == "__main__":
    main()
