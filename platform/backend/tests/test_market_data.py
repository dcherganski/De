import json
from datetime import timedelta
from decimal import Decimal

from app.core.numbers import floor_to_step
from app.market_data.candles import CandleAggregator
from app.market_data.fixture import load_fixture_events
from app.market_data.kraken import parse_ohlc, parse_ws_message
from app.market_data.state import MarketState
from app.market_data.types import TradeTick

from .conftest import T0, make_quote

# Message shapes copied from the official Kraken WS v2 documentation examples (symbol adapted).
KRAKEN_TICKER = {
    "channel": "ticker", "type": "update",
    "data": [{"symbol": "BTC/EUR", "bid": 61234.5, "bid_qty": 0.25, "ask": 61234.6, "ask_qty": 1.5,
              "last": 61234.6, "volume": 1234.56, "vwap": 61000.1, "low": 60000.0, "high": 62000.0,
              "change": 100.0, "change_pct": 0.16, "timestamp": "2026-01-05T12:00:00.123456Z"}],
}
KRAKEN_TRADE = {
    "channel": "trade", "type": "update",
    "data": [{"symbol": "BTC/EUR", "side": "sell", "price": 61234.5, "qty": 0.0123, "ord_type": "market",
              "trade_id": 4665906, "timestamp": "2026-01-05T12:00:01.708706Z"}],
}


def test_floor_to_step():
    assert floor_to_step(Decimal("0.123456789"), Decimal("0.00000001")) == Decimal("0.12345678")
    assert floor_to_step(Decimal("1.99"), Decimal("0.5")) == Decimal("1.5")


def test_kraken_ticker_parsed_exactly_as_decimal():
    (q,) = parse_ws_message(json.dumps(KRAKEN_TICKER), "BTC/EUR", received=T0)
    assert q.venue == "kraken" and not q.is_fixture
    assert q.bid == Decimal("61234.5") and q.ask == Decimal("61234.6")
    assert q.ask_qty == Decimal("1.5")
    assert q.ts_exchange.isoformat().startswith("2026-01-05T12:00:00.123456")
    assert q.spread == Decimal("0.1")


def test_kraken_trade_and_other_symbols():
    (t,) = parse_ws_message(json.dumps(KRAKEN_TRADE), "BTC/EUR", received=T0)
    assert t.price == Decimal("61234.5") and t.qty == Decimal("0.0123") and t.side == "sell"
    assert parse_ws_message(json.dumps(KRAKEN_TRADE), "ETH/EUR", received=T0) == []
    assert parse_ws_message(json.dumps({"channel": "heartbeat"}), "BTC/EUR") == []


def test_kraken_ohlc_marks_open_candle():
    # Shape from docs.kraken.com/exchange/guides/general/historical-data
    t1 = int((T0 - timedelta(minutes=10)).timestamp())
    t2 = int((T0 - timedelta(minutes=2)).timestamp())
    payload = {"error": [], "result": {"XXBTZEUR": [
        [t1, "60000.0", "60100.0", "59900.0", "60050.0", "60010.0", "1.5", 20],
        [t2, "60050.0", "60070.0", "60040.0", "60060.0", "60055.0", "0.2", 3],
    ], "last": t2}}
    bars = parse_ohlc(payload, "BTC/EUR", 300, now=T0)
    assert [b.closed for b in bars] == [True, False]
    assert bars[0].close == Decimal("60050.0") and bars[0].trade_count == 20


def _trade(sec: int, price: str) -> TradeTick:
    ts = T0 + timedelta(seconds=sec)
    return TradeTick("fixture:test", "BTC/EUR", Decimal(price), Decimal("0.1"), "buy", ts, ts, is_fixture=True)


def test_candle_aggregator_closes_on_next_interval_and_on_clock():
    agg = CandleAggregator("fixture:test", "BTC/EUR", 300, is_fixture=True)
    assert agg.add_trade(_trade(10, "100")) is None
    agg.add_trade(_trade(20, "105"))
    agg.add_trade(_trade(30, "95"))
    closed = agg.add_trade(_trade(301, "101"))
    assert closed is not None and closed.closed
    assert (closed.open, closed.high, closed.low, closed.close) == (Decimal(100), Decimal(105), Decimal(95), Decimal(95))
    assert closed.volume == Decimal("0.3") and closed.trade_count == 3
    assert agg.add_trade(_trade(50, "999")) is None  # late trade for a closed interval is ignored
    assert agg.current.high == Decimal("101")
    assert agg.tick(T0 + timedelta(seconds=599)) is None
    closed2 = agg.tick(T0 + timedelta(seconds=600))
    assert closed2 is not None and closed2.open_time == T0 + timedelta(seconds=300)


def test_stale_and_missing_data_block_trading():
    m = MarketState("kraken", "BTC/EUR")
    check = m.check(15, 420, now=T0)
    assert not check.ok and any("котировка" in r for r in check.reasons)
    m.feed_connected = True
    m.update(make_quote("100", "101", ts=T0 - timedelta(seconds=30)))
    check = m.check(15, 420, now=T0)
    assert any("остаряла" in r for r in check.reasons)
    assert any("свещ" in r for r in check.reasons)
    m.update(make_quote("100", "101", ts=T0))
    check = m.check(15, 420, now=T0, require_candle=False)
    assert check.ok


def test_insane_quote_is_ignored():
    m = MarketState("kraken", "BTC/EUR")
    m.update(make_quote("101", "100"))  # crossed book
    assert m.quote is None


def test_fixture_file_is_labelled(tmp_path):
    f = tmp_path / "sample.jsonl"
    f.write_text('# SYNTHETIC\n{"type": "quote", "t": "2026-01-05T12:00:00Z", "bid": "1", "ask": "2"}\n'
                 '{"type": "trade", "t": "2026-01-05T12:00:01Z", "price": "1.5", "qty": "3"}\n')
    events = load_fixture_events(f, "BTC/EUR")
    assert len(events) == 2 and all(e.is_fixture for e in events)
    assert events[0].venue == "fixture:sample.jsonl"
