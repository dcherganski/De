"""What actually happened after each forecast, and how well the probabilities were calibrated.

For a signal made at t0 with horizon H:
- close_above: mid price at t0+H (nearest stored quote within ±60 s) ≥ the target level;
- touch: any stored mid price or candle high within (t0, t0+H] ≥ the target level;
- close_below: mid price at t0+H ≤ the stop level.
Signals without data around t0+H are closed as "no data" after two hours and excluded from scoring.
"""

from __future__ import annotations

import math
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select

from ..core.models import Candle, QuoteSnapshot, Signal

TOLERANCE = timedelta(seconds=60)
GIVE_UP_AFTER = timedelta(hours=2)


def _mid_at(session, symbol: str, ts) -> Decimal | None:
    rows = session.execute(
        select(QuoteSnapshot.bid, QuoteSnapshot.ask, QuoteSnapshot.ts_received)
        .where(QuoteSnapshot.symbol == symbol, QuoteSnapshot.ts_received.between(ts - TOLERANCE, ts + TOLERANCE))
    ).all()
    if not rows:
        return None
    bid, ask, _ = min(rows, key=lambda r: abs((r[2] - ts).total_seconds()))
    return (bid + ask) / 2


def _max_between(session, symbol: str, venue: str, start, end) -> Decimal | None:
    q_max = session.scalar(
        select(func.max((QuoteSnapshot.bid + QuoteSnapshot.ask) / 2))
        .where(QuoteSnapshot.symbol == symbol, QuoteSnapshot.ts_received > start, QuoteSnapshot.ts_received <= end)
    )
    c_max = session.scalar(
        select(func.max(Candle.high)).where(Candle.symbol == symbol, Candle.venue == venue,
                                            Candle.open_time >= start, Candle.open_time < end)
    )
    values = [v for v in (q_max, c_max) if v is not None]
    return max(values) if values else None


def record_outcomes(sf, now) -> int:
    done = 0
    with sf.begin() as session:
        pending = session.scalars(
            select(Signal).where(Signal.outcome_recorded_at.is_(None), Signal.horizon_end + TOLERANCE <= now)
        ).all()
        for sig in pending:
            mid = _mid_at(session, sig.symbol, sig.horizon_end)
            if mid is None:
                if now - sig.horizon_end >= GIVE_UP_AFTER:
                    sig.outcome_recorded_at, sig.outcome_note = now, "няма данни около края на хоризонта"
                    done += 1
                continue
            target = sig.p0_mid * (1 + sig.target_pct / 100)
            stop = sig.p0_mid * (1 - sig.stop_pct / 100)
            high = _max_between(session, sig.symbol, sig.venue, sig.created_at, sig.horizon_end)
            sig.outcome_mid = mid
            sig.outcome_close_above = mid >= target
            sig.outcome_close_below = mid <= stop
            sig.outcome_touch = bool(high is not None and high >= target) or mid >= target
            sig.outcome_recorded_at = now
            done += 1
    return done


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for k successes in n trials; stays inside [0, 1] even for tiny n."""
    if n == 0:
        return 0.0, 1.0
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return max(0.0, centre - half), min(1.0, centre + half)


def reliability_bins(pairs: list[tuple[float, float]], bins: int = 10) -> list[dict]:
    """Forecasts grouped into equal-width probability bins (only non-empty ones), for a reliability diagram."""
    groups: dict[int, list[tuple[float, float]]] = {}
    for p, o in pairs:
        groups.setdefault(min(bins - 1, max(0, int(p * bins))), []).append((p, o))
    out = []
    for i in sorted(groups):
        g = groups[i]
        k = int(sum(o for _, o in g))
        lo, hi = wilson(k, len(g))
        out.append({"lo": i / bins, "hi": (i + 1) / bins, "n": len(g), "hits": k,
                    "mean_forecast": sum(p for p, _ in g) / len(g), "observed_rate": k / len(g),
                    "ci_lo": lo, "ci_hi": hi})
    return out


def calibration(sf) -> dict:
    """Brier scores and frequencies. With few signals these numbers are noise, and say so."""
    with sf() as session:
        rows = session.execute(
            select(Signal.p_close_above, Signal.outcome_close_above, Signal.p_touch, Signal.outcome_touch,
                   Signal.p_close_below, Signal.outcome_close_below)
            .where(Signal.valid.is_(True), Signal.outcome_close_above.is_not(None))
        ).all()
    out: dict = {"n": len(rows)}
    for name, pi, oi in (("close_above", 0, 1), ("touch", 2, 3), ("close_below", 4, 5)):
        pairs = [(float(r[pi]), 1.0 if r[oi] else 0.0) for r in rows if r[pi] is not None and r[oi] is not None]
        if not pairs:
            out[name] = None
            continue
        brier = sum((p - o) ** 2 for p, o in pairs) / len(pairs)
        base = sum(o for _, o in pairs) / len(pairs)
        ref = sum((base - o) ** 2 for _, o in pairs) / len(pairs)
        out[name] = {"n": len(pairs), "mean_forecast": sum(p for p, _ in pairs) / len(pairs), "observed_rate": base,
                     "brier": brier, "brier_of_base_rate": ref, "bins": reliability_bins(pairs)}
    out["note"] = ("Под ~100 сигнала тези числа са шум и не показват умение."
                   if len(rows) < 100 else "Оценка извън извадката; миналото не гарантира бъдещето.")
    return out
