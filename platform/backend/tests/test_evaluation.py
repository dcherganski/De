"""Soak sampling and the point-in-time evaluation, on SYNTHETIC data."""

from datetime import timedelta
from decimal import Decimal

from app.core.models import Signal
from app.evaluation.historical import Row, evaluate, load_rows, report_md
from app.evaluation.soak import collect, summarize

from .conftest import T0, synthetic_events
from .test_engine_flow import run_engine


async def test_soak_sample_and_partial_report(sf, settings):
    await run_engine(sf, settings, synthetic_events(minutes=6))
    engine_time = T0 + timedelta(minutes=6)
    from app.core.models import EngineStatus

    with sf.begin() as s:
        s.get(EngineStatus, 1).heartbeat_at = engine_time
    a = collect(sf, engine_time)
    b = collect(sf, engine_time + timedelta(hours=1))
    assert a.engine_alive and not b.engine_alive and a.is_fixture
    text = summarize([a, b], planned_hours=24, sf=sf)
    assert "НЕ е пълен" in text and "1.00 ч" in text and "Тестови данни (fixture) в някоя проба: да" in text


def rows_with_edge(n, edge_from):
    out = []
    for i in range(n):
        p = Decimal("0.80") if i % 2 else Decimal("0.52")
        good = p >= Decimal("0.75") and i >= edge_from
        out.append(Row(f"2026-01-{1 + i // 48:02d}T{(i % 48) // 2:02d}:{30 * (i % 2):02d}:00+00:00", p,
                       Decimal("1.5") if good else Decimal("-0.8"), good))
    return out


def test_threshold_is_chosen_on_tuning_period_only():
    rows = rows_with_edge(200, edge_from=0)
    res = evaluate(rows, 0.6)
    assert res["chosen_threshold"] is not None and Decimal(res["chosen_threshold"]) > Decimal("0.52")
    assert res["evaluation"]["trades"] > 0 and res["evaluation"]["sum_net_pct"] > 0
    assert res["tuning_period"][1] < res["evaluation_period"][0]  # strictly earlier, never shuffled
    # An edge that only appears in the evaluation period cannot be "found" by tuning.
    late = evaluate(rows_with_edge(200, edge_from=150), 0.6)
    assert late["chosen_threshold"] is None and late["evaluation"]["trades"] == 0
    assert "Избран праг" in report_md(late) and "няма" in report_md(late)


async def test_load_rows_charges_costs(sf, settings):
    with sf.begin() as s:
        s.add(Signal(created_at=T0, trigger="candle_close", venue="fixture:test", symbol="BTC/EUR",
                     p0_bid=Decimal("60000"), p0_ask=Decimal("60010"), p0_mid=Decimal("60005"),
                     target_pct=Decimal("1"), stop_pct=Decimal("0.5"), horizon_end=T0 + timedelta(minutes=30),
                     action="HOLD", valid=True, config_version="test", p_close_above=Decimal("0.6"),
                     outcome_mid=Decimal("60005"), outcome_close_above=False, outcome_recorded_at=T0))
    (row,) = load_rows(sf, settings)
    assert row.net_return_pct < Decimal("-0.5")  # flat price still loses the round-trip costs
