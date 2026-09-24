"""Prediction journal: every forecast is logged, and later scored against what happened.

This is the bot's memory of its own track record - the live, out-of-sample complement
to the backtest.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .bot import Prediction
from .data import granularity_seconds

JOURNAL_COLUMNS = [
    "created_at", "product", "granularity", "horizon", "as_of", "target_close_time",
    "last_close", "prob_up", "exp_return", "expected_price", "low_68", "high_68",
    "low_95", "high_95", "signal", "has_edge",
]


def append_prediction(path: str | Path, pred: Prediction, created_at: datetime | None = None) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = {
        "created_at": (created_at or datetime.now(timezone.utc)).isoformat(),
        "product": pred.product,
        "granularity": pred.granularity,
        "horizon": pred.horizon,
        "as_of": pred.as_of,
        "target_close_time": pred.target_close_time,
        "last_close": pred.last_close,
        "prob_up": pred.prob_up,
        "exp_return": pred.exp_return,
        "expected_price": pred.expected_price,
        "low_68": pred.range_68[0],
        "high_68": pred.range_68[1],
        "low_95": pred.range_95[0],
        "high_95": pred.range_95[1],
        "signal": pred.signal,
        "has_edge": pred.has_edge,
    }
    frame = pd.DataFrame([row], columns=JOURNAL_COLUMNS)
    if path.exists():
        existing = pd.read_csv(path)
        # One entry per (product, granularity, horizon, as_of): re-running replaces it.
        key = ["product", "granularity", "horizon", "as_of"]
        same = (existing[key].astype(str) == frame[key].astype(str).iloc[0]).all(axis=1)
        frame = pd.concat([existing[~same], frame], ignore_index=True)
    frame.to_csv(path, index=False)


def evaluate_journal(
    path: str | Path, candles: pd.DataFrame, granularity: str, product: str | None = None
) -> pd.DataFrame:
    """Attach the realised close to every matured prediction in the journal."""
    log = pd.read_csv(path)
    log = log[log["granularity"] == granularity]
    if product:
        log = log[log["product"] == product]
    log = log.reset_index(drop=True)
    bar = pd.Timedelta(seconds=granularity_seconds(granularity))
    by_close_time = pd.Series(candles["close"].to_numpy(), index=candles.index + bar)
    target = pd.to_datetime(log["target_close_time"], utc=True)
    log["actual_close"] = by_close_time.reindex(target).to_numpy()
    matured = log["actual_close"].notna()
    for col in ("direction_hit", "inside_68", "inside_95"):
        log[col] = pd.Series(pd.NA, index=log.index, dtype="boolean")
    m = log[matured]
    log.loc[matured, "direction_hit"] = (m["prob_up"] > 0.5) == (m["actual_close"] > m["last_close"])
    log.loc[matured, "inside_68"] = m["actual_close"].between(m["low_68"], m["high_68"])
    log.loc[matured, "inside_95"] = m["actual_close"].between(m["low_95"], m["high_95"])
    return log


def summarize_journal(evaluated: pd.DataFrame) -> dict:
    matured = evaluated[evaluated["actual_close"].notna()] if "actual_close" in evaluated else evaluated.iloc[0:0]
    n = len(matured)

    def rate(col):
        return float(matured[col].astype(float).mean()) if n else None

    return {
        "logged": int(len(evaluated)),
        "matured": int(n),
        "direction_accuracy": rate("direction_hit"),
        "coverage_68": rate("inside_68"),
        "coverage_95": rate("inside_95"),
    }
