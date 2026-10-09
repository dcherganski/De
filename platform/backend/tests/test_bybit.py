"""Bybit feed parsing and account adapter.

Fixtures marked DOCS are copied from the response examples in Bybit's official V5 documentation
(bybit-exchange.github.io/docs/v5, read 2026-10-09). They are documentation samples with old prices,
not live market data. The fake exchange below is a TEST DOUBLE that follows the documented formats.
"""

import hashlib
import hmac
import json
from decimal import Decimal
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest
from sqlalchemy import select

from app.core.models import Fill, Lot, Order
from app.core.timeutil import UTC
from app.exchange_adapters.base import BrokerError, BrokerUnavailable, OrderRequest, OrderStatus
from app.exchange_adapters.bybit import BybitAdapter, sign
from app.execution.service import ExecutionService
from app.market_data.bybit import (
    BybitError,
    ms_to_dt,
    parse_instrument,
    parse_kline,
    parse_orderbook,
    parse_ws_message,
    to_bybit_symbol,
)
from app.portfolio.service import PortfolioService

from .conftest import Clock

# DOCS: /v5/websocket/public/trade response example
DOCS_WS_TRADE = {"topic": "publicTrade.BTCUSDT", "type": "snapshot", "ts": 1672304486868, "data": [
    {"T": 1672304486865, "s": "BTCUSDT", "S": "Buy", "v": "0.001", "p": "16578.50", "L": "PlusTick",
     "i": "20f43950-d8dd-5b31-9112-a178eb6023af", "BT": False, "seq": 1783284617}]}
# DOCS shape of /v5/websocket/public/orderbook (snapshot), reduced to level 1 with the example's best levels
DOCS_WS_BOOK = {"topic": "orderbook.1.BTCUSDT", "type": "snapshot", "ts": 1672304484978,
                "data": {"s": "BTCUSDT", "b": [["16493.50", "0.006"]], "a": [["16611.00", "0.029"]],
                         "u": 18521288, "seq": 7961638724}, "cts": 1672304484976}
# DOCS: /v5/market/kline response example (inverse BTCUSD, newest first)
DOCS_KLINE = {"retCode": 0, "retMsg": "OK", "result": {"symbol": "BTCUSD", "category": "inverse", "list": [
    ["1670608800000", "17071", "17073", "17027", "17055.5", "268611", "15.74462667"],
    ["1670605200000", "17071.5", "17071.5", "17061", "17071", "4177", "0.24469757"],
    ["1670601600000", "17086.5", "17088", "16978", "17071.5", "6356", "0.37288112"]]},
    "retExtInfo": {}, "time": 1672025956592}
# DOCS: /v5/market/orderbook response example
DOCS_BOOK = {"retCode": 0, "retMsg": "OK", "result": {"s": "BTCUSDT", "a": [["65557.7", "16.606555"]],
             "b": [["65485.47", "47.081829"]], "ts": 1716863719031, "u": 230704, "seq": 1432604333,
             "cts": 1716863718905}, "retExtInfo": {}, "time": 1716863719382}
# DOCS: /v5/market/instruments-info spot response example
DOCS_INSTRUMENT = {"retCode": 0, "retMsg": "OK", "result": {"category": "spot", "list": [{
    "symbolId": 9, "symbol": "BTCUSDT", "baseCoin": "BTC", "quoteCoin": "USDT", "innovation": "0",
    "status": "Trading", "marginTrading": "utaOnly", "stTag": "0",
    "lotSizeFilter": {"basePrecision": "0.000001", "quotePrecision": "0.0000001", "minOrderQty": "0.000011",
                      "maxOrderQty": "83", "minOrderAmt": "5", "maxOrderAmt": "8000000", "maxLimitOrderQty": "83",
                      "maxMarketOrderQty": "41.5", "postOnlyMaxLimitOrderSize": "60000"},
    "priceFilter": {"tickSize": "0.1"}, "riskParameters": {"priceLimitRatioX": "0.005", "priceLimitRatioY": "0.01"},
    "symbolType": ""}]}, "retExtInfo": {}, "time": 1760027412300}
# DOCS: /v5/user/query-api response example (permissions part) — this key CAN transfer funds
DOCS_KEY_WITH_WALLET = {"retCode": 0, "retMsg": "", "result": {
    "readOnly": 1, "permissions": {"ContractTrade": ["Order", "Position"], "Spot": ["SpotTrade"],
                                   "Wallet": ["AccountTransfer", "SubMemberTransfer"], "Options": [],
                                   "Derivatives": ["DerivativesTrade"], "Exchange": ["ExchangeHistory"]},
    "ips": ["18.181.170.164"], "type": 1, "uta": 1}}

FAKE_KEY = "TEST-bybit-key-not-real"
FAKE_SECRET = "TEST-bybit-secret-not-real"
NOW_MS = 1767614400000  # synthetic clock for signatures (2026-01-05 12:00 UTC)


def test_ws_frames_from_docs():
    (tick,) = parse_ws_message(json.dumps(DOCS_WS_TRADE), "BTC/USDT")
    assert tick.price == Decimal("16578.50") and tick.qty == Decimal("0.001") and tick.side == "buy"
    assert tick.trade_id == "20f43950-d8dd-5b31-9112-a178eb6023af" and tick.ts_exchange == ms_to_dt(1672304486865)
    (q,) = parse_ws_message(DOCS_WS_BOOK, "BTC/USDT")
    assert (q.bid, q.ask, q.bid_qty, q.ask_qty) == (Decimal("16493.50"), Decimal("16611.00"), Decimal("0.006"),
                                                    Decimal("0.029"))
    assert q.venue == "bybit" and not q.is_fixture and q.ts_exchange.tzinfo is UTC
    assert parse_ws_message(DOCS_WS_BOOK, "BTC/EUR") == []  # other symbol
    assert parse_ws_message({"success": True, "ret_msg": "pong", "op": "ping"}, "BTC/USDT") == []
    empty = json.loads(json.dumps(DOCS_WS_BOOK))
    empty["data"]["a"] = []
    assert parse_ws_message(empty, "BTC/USDT") == []  # one-sided book: no quote


def test_rest_parsers_from_docs():
    bars = parse_kline(DOCS_KLINE, "BTC/USD", 3600, now=ms_to_dt(1670609000000))
    assert [b.open_time for b in bars] == sorted(b.open_time for b in bars)  # oldest first
    assert bars[0].open == Decimal("17086.5") and bars[-1].close == Decimal("17055.5")
    assert all(b.closed for b in bars[:-1]) and not bars[-1].closed  # newest still open at `now`
    q = parse_orderbook(DOCS_BOOK, "BTC/USDT")
    assert q.bid == Decimal("65485.47") and q.ask == Decimal("65557.7")
    rules = parse_instrument(DOCS_INSTRUMENT, "BTC/USDT")
    assert rules.qty_step == Decimal("0.000001") and rules.min_cost == Decimal("5") and rules.price_tick == Decimal("0.1")
    with pytest.raises(BybitError):
        parse_instrument(DOCS_INSTRUMENT, "BTC/EUR")
    with pytest.raises(BybitError) as err:
        parse_kline({"retCode": 10006, "retMsg": "Too many visits"}, "BTC/USD", 300)
    assert err.value.code == 10006
    assert to_bybit_symbol("btc/eur") == "BTCEUR"


def test_signature_matches_documented_recipe():
    payload = "category=option&symbol=BTC-29JUL22-25000-C"  # DOCS example query
    expected = hmac.new(b"secret", f"1658384314791XXXXXXXXXX5000{payload}".encode(), hashlib.sha256).hexdigest()
    assert sign("secret", "1658384314791", "XXXXXXXXXX", "5000", payload) == expected


class FakeBybit:
    """TEST DOUBLE of the documented V5 endpoints. Verifies every signature like the exchange would."""

    def __init__(self, timeout_create_after_store=False, create_code=0):
        self.orders: dict[str, dict] = {}
        self.calls: list[str] = []
        self.timeout_create_after_store = timeout_create_after_store
        self.create_code = create_code
        self.key_info = {"retCode": 0, "retMsg": "", "result": {
            "readOnly": 0, "permissions": {"Spot": ["SpotTrade"], "Wallet": []}, "ips": ["203.0.113.7"], "type": 1,
            "uta": 1}}

    def _verify(self, req: httpx.Request) -> bool:
        payload = req.content.decode() if req.method == "POST" else urlsplit(str(req.url)).query
        h = req.headers
        expected = sign(FAKE_SECRET, h["X-BAPI-TIMESTAMP"], h["X-BAPI-API-KEY"], h["X-BAPI-RECV-WINDOW"], payload)
        return h.get("X-BAPI-SIGN") == expected and h["X-BAPI-API-KEY"] == FAKE_KEY

    def __call__(self, req: httpx.Request) -> httpx.Response:
        path = req.url.path
        self.calls.append(f"{req.method} {path}")
        if path.startswith("/v5/market/"):
            return httpx.Response(200, json=DOCS_INSTRUMENT if "instruments" in path else DOCS_BOOK)
        if not self._verify(req):
            return httpx.Response(200, json={"retCode": 10004, "retMsg": "Error sign"})
        params = dict(parse_qsl(urlsplit(str(req.url)).query))
        if path == "/v5/order/create":
            body = json.loads(req.content)
            if self.create_code:
                return httpx.Response(200, json={"retCode": self.create_code, "retMsg": "rejected by test double"})
            link = body["orderLinkId"]
            if link not in self.orders:
                self.orders[link] = {"body": body, "orderId": f"ord-{len(self.orders) + 1}"}
            if self.timeout_create_after_store:
                self.timeout_create_after_store = False
                raise httpx.ReadTimeout("simulated timeout after the exchange stored the order")
            return httpx.Response(200, json={"retCode": 0, "retMsg": "OK",
                                             "result": {"orderId": self.orders[link]["orderId"], "orderLinkId": link}})
        if path in ("/v5/order/realtime", "/v5/order/history"):
            o = self.orders.get(params.get("orderLinkId", ""))
            items = []
            if o and path == "/v5/order/realtime":
                b = o["body"]
                items = [{"orderId": o["orderId"], "orderLinkId": b["orderLinkId"], "symbol": b["symbol"],
                          "qty": b["qty"], "side": b["side"], "orderStatus": "Filled", "avgPrice": "60000",
                          "cumExecQty": b["qty"], "cumFeeDetail": {"BTC": str(Decimal(b["qty"]) * Decimal("0.0025"))}
                          if b["side"] == "Buy" else {"EUR": "0.15"}, "rejectReason": "EC_NoError"}]
            return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": {"list": items}})
        if path == "/v5/execution/list":
            o = self.orders.get(params.get("orderLinkId", ""))
            items = []
            if o:
                b = o["body"]
                fee, cur = ((str(Decimal(b["qty"]) * Decimal("0.0025")), "BTC") if b["side"] == "Buy"
                            else ("0.15", "EUR"))
                items = [{"symbol": b["symbol"], "orderId": o["orderId"], "orderLinkId": b["orderLinkId"],
                          "side": b["side"], "execId": f"exec-{o['orderId']}", "execPrice": "60000",
                          "execQty": b["qty"], "execFee": fee, "feeCurrency": cur, "execType": "Trade",
                          "execTime": str(NOW_MS)}]
            return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": {"list": items}})
        if path == "/v5/account/wallet-balance":
            return httpx.Response(200, json={"retCode": 0, "retMsg": "OK", "result": {"list": [{"coin": [
                {"coin": "BTC", "walletBalance": "0.0029925", "locked": "0", "spotBorrow": "0"},
                {"coin": "EUR", "walletBalance": "819.70", "locked": "0", "spotBorrow": "0"}]}]}})
        if path == "/v5/order/cancel":
            return httpx.Response(200, json={"retCode": 170213, "retMsg": "Order does not exist."})
        if path == "/v5/user/query-api":
            return httpx.Response(200, json=self.key_info)
        return httpx.Response(404)


def adapter(fake, trading=False):
    return BybitAdapter("BTC/EUR", region="eu", credentials={"api_key": FAKE_KEY, "api_secret": FAKE_SECRET},
                        trading_enabled=trading, transport=httpx.MockTransport(fake), now_ms=lambda: NOW_MS)


async def test_trading_is_locked_by_default():
    fake = FakeBybit()
    a = adapter(fake)
    assert a.base_url == "https://api.bybit.eu"
    with pytest.raises(BrokerUnavailable):
        await a.place_order(OrderRequest("cid-1", "BTC/EUR", "buy", Decimal("0.001")))
    with pytest.raises(BrokerUnavailable):
        await a.cancel_order("cid-1")
    assert fake.calls == []  # nothing was sent
    st = a.status()
    assert not st.trading and not st.live_trading_verified and "заключена" in st.reason
    assert "cid" not in repr(a.client) and FAKE_SECRET not in repr(a.client)


async def test_key_check_refuses_transfer_rights():
    fake = FakeBybit()
    ok = await adapter(fake).check_key()
    assert ok["ok"] and ok["can_trade_spot"] and ok["ip_restricted"] and ok["key_type"] == "личен"
    fake.key_info = DOCS_KEY_WITH_WALLET
    bad = await adapter(fake).check_key()
    assert not bad["ok"] and any("Wallet" in p for p in bad["problems"]) and any("излишни" in p for p in bad["problems"])


async def test_order_body_and_status_mapping():
    fake = FakeBybit()
    a = adapter(fake, trading=True)
    body = a.order_body(OrderRequest("3f2a9c1e-0000-4000-8000-000000000001", "BTC/EUR", "buy", Decimal("0.00100000")))
    assert body == {"category": "spot", "symbol": "BTCEUR", "side": "Buy", "orderType": "Market", "qty": "0.001",
                    "marketUnit": "baseCoin", "orderLinkId": "3f2a9c1e-0000-4000-8000-000000000001",
                    "isLeverage": 0, "timeInForce": "IOC"}
    with pytest.raises(BrokerError):
        a.order_body(OrderRequest("x" * 37, "BTC/EUR", "buy", Decimal("0.001")))
    assert (await a.get_order_status("unknown-id")).status == OrderStatus.NOT_FOUND
    assert "GET /v5/order/history" in fake.calls  # looked in history before saying "not found"
    st = await a.place_order(OrderRequest("cid-2", "BTC/EUR", "buy", Decimal("0.001")))
    assert st.status == OrderStatus.OPEN and st.exchange_order_id
    st = await a.get_order_status("cid-2")
    assert st.status == OrderStatus.FILLED and st.fee_quote == Decimal("0.0000025") * 60000
    (fill,) = await a.get_fills("cid-2")
    assert fill.qty == Decimal("0.001") - Decimal("0.0000025")  # buy fee is taken in BTC
    assert fill.fee_quote == Decimal("0.0000025") * Decimal("60000")
    bals = await a.get_balances()
    assert bals["EUR"].free == Decimal("819.70") and bals["BTC"].free == Decimal("0.0029925")


async def test_rejections_and_unknown_errors():
    rejected = await adapter(FakeBybit(create_code=170131), trading=True).place_order(
        OrderRequest("cid-3", "BTC/EUR", "buy", Decimal("1")))
    assert rejected.status == OrderStatus.REJECTED and "170131" in rejected.reject_reason
    with pytest.raises(BrokerError):  # unknown code: outcome uncertain, the caller must check status
        await adapter(FakeBybit(create_code=10016), trading=True).place_order(
            OrderRequest("cid-4", "BTC/EUR", "buy", Decimal("1")))


async def test_timeout_after_bybit_stored_order_is_not_resent(sf, settings):
    """End to end with the execution service: a lost ack is resolved by asking for the status first."""
    fake = FakeBybit(timeout_create_after_store=True)
    a = adapter(fake, trading=True)
    s = settings.model_copy(update={"quote_venue": "bybit"})
    clock = Clock()
    ex = ExecutionService(sf, a, s, PortfolioService(sf, s, account="bybit", clock=clock), clock=clock,
                          submit_timeout_s=2)
    order = await ex.submit(side="buy", qty=Decimal("0.003"), intent="BUY")
    assert order.status == "filled" and order.exchange_order_id == "ord-1"
    assert fake.calls.count("POST /v5/order/create") == 1  # found by orderLinkId, never sent twice
    with sf() as session:
        fill = session.scalar(select(Fill))
        assert fill.venue == "bybit" and fill.qty == Decimal("0.003") - Decimal("0.0000075")
        lot = session.scalar(select(Lot).where(Lot.initial.is_(False)))
        assert lot.qty_open == fill.qty and lot.entry_price == Decimal("60000")
        assert session.scalar(select(Order)).mode == "paper"  # the settings mode is still PAPER


async def test_public_rules_from_docs_shape():
    rules = await adapter(FakeBybit()).get_market_rules("BTC/USDT")
    assert rules.min_cost == Decimal("5") and rules.source.startswith("Bybit")
    q = await adapter(FakeBybit()).get_quotes("BTC/USDT")
    assert q.ask > q.bid
