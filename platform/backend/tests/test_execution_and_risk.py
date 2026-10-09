"""Execution reliability and risk limits, against the paper broker on a real database."""

from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from app.core.models import AuditEntry, Fill, Lot, Order, PaperOrder
from app.exchange_adapters.base import BrokerTimeout, OrderRequest, OrderStatus
from app.execution.service import DuplicateOrder, ExecutionService
from app.paper_broker.broker import PaperBroker
from app.portfolio.service import PortfolioService
from app.risk.engine import MANUAL_SELL, RiskContext, RiskEngine

from .conftest import T0, Clock, QuoteBox, make_quote


class FlakyBroker(PaperBroker):
    """Paper broker whose first place_order times out, either after or before reaching the 'exchange'."""

    def __init__(self, *a, reach_exchange: bool, **kw):
        super().__init__(*a, **kw)
        self.reach_exchange = reach_exchange
        self.calls = 0

    async def place_order(self, req: OrderRequest):
        self.calls += 1
        if self.calls == 1:
            if self.reach_exchange:
                await super().place_order(req)
            raise BrokerTimeout("simulated network timeout")
        return await super().place_order(req)


def setup(sf, settings, broker_cls=PaperBroker, cash="1000", btc="0", **bkw):
    clock = Clock()
    box = QuoteBox(make_quote("60000", "60010", bid_qty="10", ask_qty="10"))
    s = settings.model_copy(update={"paper_partial_fills": False})
    b = broker_cls(sf, s, box, clock=clock, **bkw)
    b.init_balances({"BTC": Decimal(btc), "EUR": Decimal(cash)})
    pf = PortfolioService(sf, s, account=b.name, clock=clock)
    ex = ExecutionService(sf, b, s, pf, clock=clock, submit_timeout_s=2)
    return ex, b, box, clock


async def test_submit_records_fills_lot_and_balances(sf, settings):
    ex, b, _, _ = setup(sf, settings)
    order = await ex.submit(side="buy", qty=Decimal("0.001"), intent="BUY")
    assert order.status == "filled" and order.filled_qty == Decimal("0.001")
    with sf() as s:
        assert s.scalar(select(func.count(Fill.id))) == 1
        lot = s.scalar(select(Lot))
        assert lot.qty_open == Decimal("0.001") and lot.target_price > lot.entry_price > lot.stop_price
        kinds = [a.kind for a in s.scalars(select(AuditEntry).order_by(AuditEntry.id))]
    assert kinds[0] == "order_created" and "fill" in kinds


async def test_local_duplicate_guard(sf, settings):
    ex, b, box, _ = setup(sf, settings)
    box.quote = make_quote("60000", "60010", bid_qty="0.0001", ask_qty="0.0001")  # forces a partial fill
    s2 = ex.s.model_copy(update={"paper_partial_fills": True})
    ex.broker.s = s2
    order = await ex.submit(side="buy", qty=Decimal("0.001"), intent="BUY")
    assert order.status == "partially_filled"
    with pytest.raises(DuplicateOrder):
        await ex.submit(side="buy", qty=Decimal("0.001"), intent="BUY")


async def test_timeout_after_exchange_got_it_does_not_resend(sf, settings):
    ex, b, _, _ = setup(sf, settings, broker_cls=FlakyBroker, reach_exchange=True)
    order = await ex.submit(side="buy", qty=Decimal("0.001"), intent="BUY")
    assert order.status == "filled"
    assert b.calls == 1  # status check found it; no second place_order
    with sf() as s:
        assert s.scalar(select(func.count(PaperOrder.id))) == 1
        assert s.scalar(select(AuditEntry).where(AuditEntry.kind == "order_status_recovered")) is not None


async def test_timeout_before_exchange_resends_with_same_id(sf, settings):
    ex, b, _, _ = setup(sf, settings, broker_cls=FlakyBroker, reach_exchange=False)
    order = await ex.submit(side="buy", qty=Decimal("0.001"), intent="BUY")
    assert order.status == "filled" and b.calls == 2
    with sf() as s:
        (po,) = s.scalars(select(PaperOrder)).all()
        assert po.client_order_id == order.client_order_id
        assert s.scalar(select(AuditEntry).where(AuditEntry.kind == "order_resubmitted")) is not None


async def test_restart_reconciles_unsent_and_unknown_orders(sf, settings):
    ex, b, _, clock = setup(sf, settings)
    # 1) written locally but the process died before sending
    with sf.begin() as s:
        s.add(Order(client_order_id="never-sent", venue="paper", mode="paper", symbol="BTC/EUR", side="buy",
                    qty=Decimal("0.001"), status="new", intent="BUY", created_at=clock(), updated_at=clock()))
    # 2) sent, exchange filled it, then the process died before recording the result
    await b.place_order(OrderRequest("sent-then-crash", "BTC/EUR", "buy", Decimal("0.001")))
    with sf.begin() as s:
        s.add(Order(client_order_id="sent-then-crash", venue="paper", mode="paper", symbol="BTC/EUR", side="buy",
                    qty=Decimal("0.001"), status="unknown", intent="BUY", created_at=clock(), updated_at=clock()))
    # a fresh service, as after a restart
    ex2 = ExecutionService(sf, b, ex.s, ex.portfolio, clock=clock)
    assert await ex2.reconcile() == 2
    with sf() as s:
        o1 = s.scalar(select(Order).where(Order.client_order_id == "never-sent"))
        o2 = s.scalar(select(Order).where(Order.client_order_id == "sent-then-crash"))
        assert o1.status == "cancelled"
        assert o2.status == "filled" and o2.filled_qty == Decimal("0.001")
        assert s.scalar(select(func.count(PaperOrder.id))) == 1  # nothing was sent twice


async def test_cancel_pending(sf, settings):
    ex, b, box, _ = setup(sf, settings)
    box.quote = make_quote("60000", "60010", bid_qty="0.0001", ask_qty="0.0001")
    ex.broker.s = ex.s.model_copy(update={"paper_partial_fills": True})
    await ex.submit(side="buy", qty=Decimal("0.001"), intent="BUY")
    done = await ex.cancel_pending("tester")
    assert len(done) == 1
    with sf() as s:
        assert s.scalar(select(Order)).status == "cancelled"
    assert (await b.get_balances())["EUR"].locked == 0


# ---------------------------------------------------------------------- risk
def ctx(settings, **kw):
    base = dict(now=T0, quote=make_quote("60000", "60010"), data_ok=True, data_reasons=[], cash_free=Decimal("500"),
                base_free=Decimal("0"), base_total=Decimal("0"), open_orders=0, trades_today=0, last_trade_at=None,
                day_start_equity=Decimal("500"), equity_now=Decimal("500"), entries_paused=False,
                rules=PaperBroker(None, settings, lambda: None).rules("BTC/EUR"), signal_age_s=5.0)
    base.update(kw)
    return RiskContext(**base)


def test_buy_sized_by_max_order_and_rounded_to_step(settings):
    r = RiskEngine(settings).evaluate_buy(ctx(settings))
    assert r.allowed
    assert r.notional_est <= settings.max_order_eur
    assert r.qty == r.qty.quantize(Decimal("0.00000001"))


@pytest.mark.parametrize("override, reason", [
    ({"cash_free": Decimal("0")}, "свободни средства"),
    ({"data_ok": False, "data_reasons": ["котировката е остаряла"]}, "остаряла"),
    ({"quote": make_quote("60000", "61000")}, "спредът"),
    ({"trades_today": 6}, "дневният лимит"),
    ({"last_trade_at": T0 - timedelta(minutes=1)}, "пауза между сделките"),
    ({"equity_now": Decimal("489")}, "дневната загуба"),
    ({"entries_paused": True}, "спрени"),
    ({"open_orders": 1}, "чакаща поръчка"),
    ({"signal_age_s": 999.0}, "сигналът е на"),
    ({"base_total": Decimal("0.01")}, "експозицията"),
])
def test_buy_blocked_with_reason(settings, override, reason):
    if "base_total" in override:
        settings = settings.model_copy(update={"max_exposure_eur": Decimal("500")})
    r = RiskEngine(settings).evaluate_buy(ctx(settings, **override))
    assert not r.allowed and r.qty == 0
    assert any(reason in x for x in r.reasons), r.reasons


def test_exits_skip_entry_limits_but_not_stale_data(settings):
    eng = RiskEngine(settings)
    c = ctx(settings, base_free=Decimal("0.001"), base_total=Decimal("0.001"), trades_today=99,
            last_trade_at=T0, equity_now=Decimal("1"))
    assert eng.evaluate_sell(c, "EXIT_SL", Decimal("0.001")).allowed
    stale = ctx(settings, base_free=Decimal("0.001"), data_ok=False, data_reasons=["няма котировка"])
    assert not eng.evaluate_sell(stale, "EXIT_SL", Decimal("0.001")).allowed


def test_pause_blocks_automatic_exits_but_not_manual_sell(settings):
    eng = RiskEngine(settings)
    c = ctx(settings, base_free=Decimal("0.001"), entries_paused=True)
    assert not eng.evaluate_sell(c, "EXIT_TP", Decimal("0.001")).allowed
    assert eng.evaluate_sell(c, MANUAL_SELL, Decimal("0.001")).allowed


def test_large_position_sold_in_slices_without_dust(settings):
    eng = RiskEngine(settings)
    c = ctx(settings, base_free=Decimal("0.0033"), base_total=Decimal("0.0033"))
    r = eng.evaluate_sell(c, MANUAL_SELL, Decimal("0.0033"))
    assert r.allowed and r.notional_est <= settings.max_order_eur and r.qty < Decimal("0.0033")
    tiny_rest = ctx(settings, base_free=Decimal("0.00168"), base_total=Decimal("0.00168"))
    r2 = eng.evaluate_sell(tiny_rest, MANUAL_SELL, Decimal("0.00168"))
    assert r2.qty == Decimal("0.00168")  # leftover would be below the minimum, so it goes too


def test_order_status_enum_final():
    assert OrderStatus.FILLED.is_final and not OrderStatus.PARTIALLY_FILLED.is_final
