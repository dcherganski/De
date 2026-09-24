"""Detection of market events and an event study of what historically followed them.

An "event" is a discrete, human-readable condition on a bar (a volume spike, a golden
cross, a breakout...). The event study measures, over the past, how the price behaved
`horizon` bars after each event type. The bot uses these statistics both as features
and as a standalone predictor.
"""

from __future__ import annotations

import re
from pathlib import Path

import numpy as np
import pandas as pd

# Bulgarian labels shown in reports; keys are the stable event identifiers.
EVENT_LABELS: dict[str, str] = {
    "volume_spike": "Скок в обема (>2σ)",
    "big_up_move": "Силен ръст (>2σ)",
    "big_down_move": "Силен спад (>2σ)",
    "golden_cross": "Golden cross (EMA20 над EMA50)",
    "death_cross": "Death cross (EMA20 под EMA50)",
    "rsi_overbought": "RSI влиза над 70 (свръхкупен)",
    "rsi_oversold": "RSI влиза под 30 (свръхпродаден)",
    "breakout_high": "Пробив над 20-периоден максимум",
    "breakdown_low": "Пробив под 20-периоден минимум",
    "macd_bull_cross": "MACD пресича сигналната линия нагоре",
    "macd_bear_cross": "MACD пресича сигналната линия надолу",
    "bb_squeeze": "Свиване на Bollinger лентите",
    "streak_up_3": "3 поредни зелени свещи",
    "streak_down_3": "3 поредни червени свещи",
}

BULLISH = {"big_up_move", "golden_cross", "rsi_oversold", "breakout_high", "macd_bull_cross", "streak_up_3"}
BEARISH = {"big_down_move", "death_cross", "rsi_overbought", "breakdown_low", "macd_bear_cross", "streak_down_3"}


def _cross_above(a: pd.Series, b: pd.Series | float) -> pd.Series:
    b_prev = b.shift(1) if isinstance(b, pd.Series) else b
    return (a > b) & (a.shift(1) <= b_prev)


def _cross_below(a: pd.Series, b: pd.Series | float) -> pd.Series:
    b_prev = b.shift(1) if isinstance(b, pd.Series) else b
    return (a < b) & (a.shift(1) >= b_prev)


def detect_events(ind: pd.DataFrame) -> pd.DataFrame:
    """Boolean event matrix for a frame produced by `indicators.add_indicators`."""
    up = ind["close"] > ind["open"]
    down = ind["close"] < ind["open"]
    bw_rank = ind["bb_bandwidth"].rolling(120, min_periods=60).rank(pct=True)
    events = pd.DataFrame(
        {
            "volume_spike": ind["vol_z"] > 2.0,
            "big_up_move": ind["ret_z"] > 2.0,
            "big_down_move": ind["ret_z"] < -2.0,
            "golden_cross": _cross_above(ind["ema_fast"], ind["ema_slow"]),
            "death_cross": _cross_below(ind["ema_fast"], ind["ema_slow"]),
            "rsi_overbought": _cross_above(ind["rsi"], 70.0),
            "rsi_oversold": _cross_below(ind["rsi"], 30.0),
            "breakout_high": ind["close"] > ind["high_n"],
            "breakdown_low": ind["close"] < ind["low_n"],
            "macd_bull_cross": _cross_above(ind["macd"], ind["macd_signal"]),
            "macd_bear_cross": _cross_below(ind["macd"], ind["macd_signal"]),
            "bb_squeeze": bw_rank <= 0.10,
            "streak_up_3": up & up.shift(1, fill_value=False) & up.shift(2, fill_value=False),
            "streak_down_3": down & down.shift(1, fill_value=False) & down.shift(2, fill_value=False),
        },
        index=ind.index,
    )
    return events.fillna(False).astype(bool)


def _slug(name: str) -> str:
    return re.sub(r"[^0-9a-zA-Z]+", "_", name.strip().lower()).strip("_") or "event"


def load_external_events(path: str | Path, index: pd.DatetimeIndex) -> pd.DataFrame:
    """Map a CSV of dated external events (columns: date,name) onto bars.

    Each event is flagged on the bar that contains its timestamp, so the model can learn
    how the market reacted to e.g. "fomc", "halving" or "etf_decision" in the past.
    """
    raw = read_events_file(path)
    names = raw["name"].map(lambda n: "ext_" + _slug(n))
    out = pd.DataFrame(False, index=index, columns=sorted(set(names)))
    if len(index) == 0:
        return out
    bar = pd.Series(index).diff().median() if len(index) > 1 else pd.Timedelta(days=1)
    for when, col in zip(raw["date"], names):
        pos = index.searchsorted(when, side="right") - 1
        # Only flag an event that falls inside a bar we actually have.
        if 0 <= pos < len(index) and when < index[pos] + bar:
            out.iloc[pos, out.columns.get_loc(col)] = True
    return out


def read_events_file(path: str | Path) -> pd.DataFrame:
    """Read a CSV of dated external events (columns: date,name) with UTC timestamps."""
    raw = pd.read_csv(path)
    missing = {"date", "name"} - set(raw.columns)
    if missing:
        raise ValueError(f"events file {path} is missing columns: {sorted(missing)}")
    return pd.DataFrame(
        {"date": pd.to_datetime(raw["date"], utc=True, format="mixed"), "name": raw["name"].astype(str)}
    )


def upcoming_events(path: str | Path, after: pd.Timestamp, until: pd.Timestamp) -> list[dict]:
    """External events scheduled inside the forecast window (after, until]."""
    raw = read_events_file(path)
    window = raw[(raw["date"] > after) & (raw["date"] <= until)].sort_values("date")
    return [
        {"time": when.isoformat(), "event": "ext_" + _slug(name), "label": event_label("ext_" + _slug(name))}
        for when, name in zip(window["date"], window["name"])
    ]


def event_label(name: str) -> str:
    if name in EVENT_LABELS:
        return EVENT_LABELS[name]
    if name.startswith("ext_"):
        return "Външно събитие: " + name[4:].replace("_", " ")
    return name


def forward_returns(close: pd.Series, horizon: int) -> pd.Series:
    """log(close[t+h] / close[t]); NaN where the future is not known yet."""
    return np.log(close.shift(-horizon) / close)


def event_study(close: pd.Series, events: pd.DataFrame, horizon: int = 1) -> pd.DataFrame:
    """What happened `horizon` bars after each event type, over the given history."""
    fwd = forward_returns(close, horizon)
    known = fwd.notna()
    base_rate = float((fwd[known] > 0).mean()) if known.any() else 0.5
    base_mean = float(fwd[known].mean()) if known.any() else 0.0
    rows = []
    for name in events.columns:
        mask = events[name] & known
        sample = fwd[mask]
        n = int(mask.sum())
        occurrences = events.index[events[name]]
        if n:
            std = float(sample.std(ddof=1)) if n > 1 else float("nan")
            t_stat = float(sample.mean() / (std / np.sqrt(n))) if n > 1 and std > 0 else float("nan")
            hit = float((sample > 0).mean())
            mean = float(sample.mean())
            median = float(sample.median())
        else:
            std = t_stat = hit = mean = median = float("nan")
        rows.append(
            {
                "event": name,
                "label": event_label(name),
                "count": n,
                "hit_rate": hit,
                "edge_vs_base": hit - base_rate if n else float("nan"),
                "mean_return": mean,
                "median_return": median,
                "std_return": std,
                "t_stat": t_stat,
                "last_seen": occurrences[-1] if len(occurrences) else pd.NaT,
            }
        )
    table = pd.DataFrame(rows).set_index("event")
    table.attrs["base_rate"] = base_rate
    table.attrs["base_mean"] = base_mean
    table.attrs["horizon"] = horizon
    return table.sort_values("count", ascending=False)


def recent_events(events: pd.DataFrame, bars: int = 10) -> list[tuple[pd.Timestamp, str]]:
    """(timestamp, event) pairs that fired within the last `bars` bars, newest first."""
    tail = events.iloc[-bars:]
    found = [(ts, name) for ts, row in tail.iterrows() for name in tail.columns if row[name]]
    return sorted(found, key=lambda item: item[0], reverse=True)


def active_events(events: pd.DataFrame, position: int = -1) -> list[str]:
    row = events.iloc[position]
    return [name for name in events.columns if bool(row[name])]
