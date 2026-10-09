"""Entry point of the engine process: `python -m app.engine.main`.

Builds the feed, broker, news provider and strategy from the environment. PAPER is the default; the
engine refuses to start in LIVE mode unless every guard is satisfied.
"""

from __future__ import annotations

import asyncio
import logging
import sys

from ..core.config import Settings, get_settings
from ..core.db import session_factory
from ..core.secrets import configure_logging
from ..market_data.fixture import FixtureFeed
from ..market_data.kraken import KrakenFeed
from ..paper_broker.broker import PaperBroker
from .runner import Engine

log = logging.getLogger("engine")


def build_feed(s: Settings):
    if s.quote_venue == "kraken":
        return KrakenFeed(s.symbol)
    if s.quote_venue == "coinbase":
        from ..market_data.coinbase import CoinbaseFeed
        return CoinbaseFeed(s.symbol)
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
    feed = build_feed(s)
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


if __name__ == "__main__":
    main()
