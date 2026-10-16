from decimal import Decimal

import pytest

from app.exchange_adapters.base import OrderRequest, OrderStatus
from app.paper_broker.broker import PaperBroker

from .conftest import Clock, QuoteBox, make_quote

pytestmark = pytest.mark.asyncio


def broker(sf, settings, quote=None, **overrides):
    s = settings.model_copy(update=overrides)
    box = QuoteBox(quote or make_quote("60000", "60010", bid_qty="10", ask_qty="10"))
    return PaperBroker(sf, s, box, clock=Clock()), box


async def test_buy_fills_at_ask_plus_slippage_with_fee(sf, settings):
    b, _ = broker(sf, settings, paper_partial_fills=False)
    b.init_balances({"BTC": Decimal("0"), "EUR": Decimal("1000")})
    st = await b.place_order(OrderRequest("c1", "BTC/EUR", "buy", Decimal("0.01")))
    assert st.status == OrderStatus.FILLED
    price = Decimal("60010") * (1 + Decimal("5") * Decimal("0.0001"))
    assert st.avg_price == price
    fee = Decimal("0.01") * price * settings.paper_fee_rate
    assert st.fee_quote == fee
    bal = await b.get_balances()
    assert bal["BTC"].free == Decimal("0.01")
    assert bal["EUR"].free == Decimal("1000") - Decimal("0.01") * price - fee
    assert bal["EUR"].locked == 0  # unused reserve released


async def test_sell_fills_at_bid_minus_slippage(sf, settings):
    b, _ = broker(sf, settings, paper_partial_fills=False)
    b.init_balances({"BTC": Decimal("0.01"), "EUR": Decimal("0")})
    st = await b.place_order(OrderRequest("s1", "BTC/EUR", "sell", Decimal("0.01")))
    price = Decimal("60000") * (1 - Decimal("0.0005"))
    assert st.status == OrderStatus.FILLED and st.avg_price == price
    bal = await b.get_balances()
    assert bal["BTC"].total == 0
    assert bal["EUR"].free == Decimal("0.01") * price * (1 - settings.paper_fee_rate)


async def test_no_buy_without_funds(sf, settings):
    b, _ = broker(sf, settings)
    b.init_balances({"BTC": Decimal("0.003"), "EUR": Decimal("0")})
    st = await b.place_order(OrderRequest("c1", "BTC/EUR", "buy", Decimal("0.001")))
    assert st.status == OrderStatus.REJECTED and "недостатъчни средства" in st.reject_reason
    assert (await b.get_balances())["EUR"].free == 0


@pytest.mark.parametrize("qty, msg", [("0.00001", "минимума"), ("0.000050001", "стъпката"), ("-1", "положително")])
async def test_exchange_rules(sf, settings, qty, msg):
    b, _ = broker(sf, settings)
    b.init_balances({"BTC": Decimal("1"), "EUR": Decimal("1000")})
    st = await b.place_order(OrderRequest("x", "BTC/EUR", "sell", Decimal(qty)))
    assert st.status == OrderStatus.REJECTED and msg in st.reject_reason


async def test_min_cost(sf, settings):
    b, _ = broker(sf, settings, quote=make_quote("100", "101"), paper_min_order_base=Decimal("0.00000001"))
    b.init_balances({"BTC": Decimal("1"), "EUR": Decimal("0")})
    st = await b.place_order(OrderRequest("x", "BTC/EUR", "sell", Decimal("0.001")))  # ~0.10 EUR
    assert st.status == OrderStatus.REJECTED and "минимума" in st.reject_reason


async def test_place_is_idempotent_on_client_id(sf, settings):
    b, _ = broker(sf, settings, paper_partial_fills=False)
    b.init_balances({"BTC": Decimal("0"), "EUR": Decimal("1000")})
    first = await b.place_order(OrderRequest("same", "BTC/EUR", "buy", Decimal("0.001")))
    again = await b.place_order(OrderRequest("same", "BTC/EUR", "buy", Decimal("0.001")))
    assert first.exchange_order_id == again.exchange_order_id
    assert (await b.get_balances())["BTC"].free == Decimal("0.001")  # bought once
    assert len(await b.get_fills("same")) == 1


async def test_partial_fills_follow_top_of_book(sf, settings):
    q = make_quote("60000", "60010", bid_qty="0.004", ask_qty="0.004")
    b, box = broker(sf, settings, quote=q)
    b.init_balances({"BTC": Decimal("0"), "EUR": Decimal("1000")})
    st = await b.place_order(OrderRequest("p", "BTC/EUR", "buy", Decimal("0.005")))
    assert st.status == OrderStatus.PARTIALLY_FILLED and st.filled_qty == Decimal("0.002")  # 50% of 0.004
    box.quote = make_quote("60000", "60010", bid_qty="1", ask_qty="1")
    assert b.on_quote(box.quote) == ["p"]
    st = await b.get_order_status("p")
    assert st.status == OrderStatus.FILLED and st.filled_qty == Decimal("0.005")
    assert len(await b.get_fills("p")) == 2


async def test_cancel_releases_reserved_funds(sf, settings):
    q = make_quote("60000", "60010", bid_qty="0.002", ask_qty="0.002")
    b, _ = broker(sf, settings, quote=q)
    b.init_balances({"BTC": Decimal("0"), "EUR": Decimal("1000")})
    st = await b.place_order(OrderRequest("p", "BTC/EUR", "buy", Decimal("0.005")))
    assert st.status == OrderStatus.PARTIALLY_FILLED
    st = await b.cancel_order("p")
    assert st.status == OrderStatus.CANCELLED
    bal = await b.get_balances()
    assert bal["EUR"].locked == 0
    spent = st.filled_qty * st.avg_price + st.fee_quote
    assert bal["EUR"].free == Decimal("1000") - spent
    assert (await b.cancel_order("nope")).status == OrderStatus.NOT_FOUND


async def test_price_jump_cancels_unaffordable_remainder(sf, settings):
    q = make_quote("100", "100", bid_qty="0.002", ask_qty="0.002")
    b, box = broker(sf, settings, quote=q, paper_min_cost_quote=Decimal("0.01"))
    b.init_balances({"BTC": Decimal("0"), "EUR": Decimal("1")})
    st = await b.place_order(OrderRequest("j", "BTC/EUR", "buy", Decimal("0.009")))
    assert st.status == OrderStatus.PARTIALLY_FILLED
    box.quote = make_quote("1000", "1000", bid_qty="1", ask_qty="1")  # price x10
    b.on_quote(box.quote)
    st = await b.get_order_status("j")
    assert st.status in (OrderStatus.CANCELLED, OrderStatus.FILLED)
    bal = await b.get_balances()
    assert bal["EUR"].free >= 0 and bal["EUR"].locked == 0


async def test_stale_quote_rejects(sf, settings):
    from datetime import timedelta

    from .conftest import T0
    b, _ = broker(sf, settings, quote=make_quote("100", "101", ts=T0 - timedelta(minutes=5)))
    b.init_balances({"BTC": Decimal("1"), "EUR": Decimal("0")})
    st = await b.place_order(OrderRequest("x", "BTC/EUR", "sell", Decimal("0.1")))
    assert st.status == OrderStatus.REJECTED and "котировка" in st.reject_reason
