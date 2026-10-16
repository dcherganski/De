"""Point-in-time evaluation of recorded Jev signals, with separate tuning and evaluation periods.

Every signal was produced live from data up to its own timestamp, and its outcome was recorded 30
minutes later, so there is no look-ahead in the inputs. Signals are split by time (never shuffled):
the threshold on P(close above target) is chosen on the earlier tuning period only, then reported on
the later evaluation period, next to "never trade". Returns include the configured fee and slippage.
With fewer than ~100 evaluation signals the result is noise, and the report says so.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select

from ..core.config import Settings
from ..core.models import Signal
from ..core.numbers import BPS

GRID = [Decimal(x) / 100 for x in range(50, 91, 5)]


@dataclass
class Row:
    ts: str
    p_close_above: Decimal
    net_return_pct: Decimal  # buy at ask(+slip, +fee), sell at outcome mid(-half spread, -slip, -fee)
    hit: bool


def load_rows(sf, s: Settings) -> list[Row]:
    slip = s.paper_slippage_bps * BPS
    fee = s.paper_fee_rate
    with sf() as session:
        sigs = session.scalars(select(Signal).where(Signal.valid.is_(True), Signal.p_close_above.is_not(None),
                                                    Signal.outcome_mid.is_not(None))
                               .order_by(Signal.created_at)).all()
    rows = []
    for g in sigs:
        half_spread = (g.p0_ask - g.p0_bid) / 2
        cost_in = g.p0_ask * (1 + slip) * (1 + fee)
        value_out = (g.outcome_mid - half_spread) * (1 - slip) * (1 - fee)
        rows.append(Row(g.created_at.isoformat(), g.p_close_above, (value_out / cost_in - 1) * 100,
                        bool(g.outcome_close_above)))
    return rows


def _stats(rows: list[Row], tau: Decimal | None) -> dict:
    taken = [r for r in rows if tau is not None and r.p_close_above >= tau]
    total = sum((r.net_return_pct for r in taken), Decimal(0))
    return {"threshold": None if tau is None else str(tau), "signals": len(rows), "trades": len(taken),
            "hit_rate": (sum(r.hit for r in taken) / len(taken)) if taken else None,
            "mean_net_pct": (total / len(taken)) if taken else None, "sum_net_pct": total}


def evaluate(rows: list[Row], tune_fraction: float = 0.6, min_trades: int = 5) -> dict:
    cut = int(len(rows) * tune_fraction)
    tune, test = rows[:cut], rows[cut:]
    best = None
    for tau in GRID:
        st = _stats(tune, tau)
        if st["trades"] >= min_trades and (best is None or st["sum_net_pct"] > best["sum_net_pct"]):
            best = st
    chosen = Decimal(best["threshold"]) if best and best["sum_net_pct"] > 0 else None
    return {
        "tuning_period": [tune[0].ts, tune[-1].ts] if tune else None,
        "evaluation_period": [test[0].ts, test[-1].ts] if test else None,
        "tuning": best,
        "chosen_threshold": None if chosen is None else str(chosen),
        "evaluation": _stats(test, chosen),
        "never_trade": _stats(test, None),
        "note": ("Под ~100 сигнала в периода за оценка резултатът е шум." if len(test) < 100 else
                 "Резултат извън периода за настройка; миналото не гарантира бъдещето."),
    }


def report_md(result: dict) -> str:
    ev, tu = result["evaluation"], result["tuning"]
    fmt = lambda v: "—" if v is None else (f"{float(v):.3f}" if not isinstance(v, str) else v)  # noqa: E731
    lines = [
        "# Исторически тест (без поглед напред)",
        "",
        f"- Период за настройка: {result['tuning_period']}",
        f"- Период за оценка: {result['evaluation_period']}",
        f"- Избран праг P(над целта): {result['chosen_threshold'] or 'няма — нито един праг не е печеливш след разходи'}",
        "",
        "| | сигнали | сделки | точност | средно нето % | общо нето % |",
        "|---|---|---|---|---|---|",
        f"| настройка (най-добър праг) | {tu['signals'] if tu else 0} | {tu['trades'] if tu else 0} | "
        f"{fmt(tu['hit_rate']) if tu else '—'} | {fmt(tu['mean_net_pct']) if tu else '—'} | "
        f"{fmt(tu['sum_net_pct']) if tu else '—'} |",
        f"| оценка | {ev['signals']} | {ev['trades']} | {fmt(ev['hit_rate'])} | {fmt(ev['mean_net_pct'])} | "
        f"{fmt(ev['sum_net_pct'])} |",
        f"| оценка: без търговия | {ev['signals']} | 0 | — | — | 0 |",
        "",
        result["note"],
    ]
    return "\n".join(lines) + "\n"
