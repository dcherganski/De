"""Turns an OHLCV frame into a supervised-learning dataset (features at t, target at t+h)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from .events import BEARISH, BULLISH, detect_events, forward_returns
from .indicators import add_indicators


@dataclass
class Dataset:
    frame: pd.DataFrame  # OHLCV + indicators, full history
    events: pd.DataFrame  # boolean event matrix aligned with `frame`
    X: pd.DataFrame  # features; rows with incomplete warm-up are dropped
    fwd_return: pd.Series  # log return over the next `horizon` bars (NaN at the end)
    horizon: int

    @property
    def y(self) -> pd.Series:
        """1.0 if the price was higher `horizon` bars later, NaN if not known yet."""
        return (self.fwd_return > 0).astype(float).where(self.fwd_return.notna())

    @property
    def labeled(self) -> pd.Index:
        return self.fwd_return.dropna().index

    @property
    def event_columns(self) -> list[str]:
        return list(self.events.columns)


def _bar_seconds(index: pd.DatetimeIndex) -> float:
    if len(index) < 2:
        return 86400.0
    return float(pd.Series(index).diff().dt.total_seconds().median())


def build_features(ind: pd.DataFrame, events: pd.DataFrame) -> pd.DataFrame:
    r = ind["log_ret"]
    close = ind["close"]
    feats = pd.DataFrame(index=ind.index)
    feats["ret_1"] = r
    for lag in range(1, 5):
        feats[f"ret_lag_{lag}"] = r.shift(lag)
    for window in (3, 7, 14):
        feats[f"ret_{window}"] = r.rolling(window).sum()
    feats["vol_7"] = r.rolling(7).std(ddof=0)
    feats["vol_30"] = r.rolling(30).std(ddof=0)
    feats["vol_ratio"] = feats["vol_7"] / feats["vol_30"].replace(0.0, np.nan)
    feats["rsi"] = ind["rsi"] / 100.0 - 0.5
    feats["macd_hist"] = ind["macd_hist"] / close
    feats["bb_pct_b"] = ind["bb_pct_b"]
    feats["bb_bandwidth"] = ind["bb_bandwidth"]
    feats["dist_ema_fast"] = close / ind["ema_fast"] - 1.0
    feats["dist_ema_slow"] = close / ind["ema_slow"] - 1.0
    feats["ema_trend"] = ind["ema_fast"] / ind["ema_slow"] - 1.0
    feats["vol_z"] = ind["vol_z"]
    feats["atr_pct"] = ind["atr_pct"]
    span = (ind["high"] - ind["low"]).replace(0.0, np.nan)
    feats["range_pct"] = span / close
    feats["close_pos"] = (close - ind["low"]) / span

    bar = _bar_seconds(ind.index)
    if bar < 86400:
        hour = ind.index.hour + ind.index.minute / 60.0
        feats["hour_sin"] = np.sin(2 * np.pi * hour / 24.0)
        feats["hour_cos"] = np.cos(2 * np.pi * hour / 24.0)
    if bar <= 86400:
        dow = ind.index.dayofweek
        feats["dow_sin"] = np.sin(2 * np.pi * dow / 7.0)
        feats["dow_cos"] = np.cos(2 * np.pi * dow / 7.0)

    ev = events.astype(float)
    for name in ev.columns:
        feats[f"ev_{name}"] = ev[name]
    bull = [c for c in ev.columns if c in BULLISH]
    bear = [c for c in ev.columns if c in BEARISH]
    feats["bull_events_5"] = ev[bull].sum(axis=1).rolling(5, min_periods=1).sum() if bull else 0.0
    feats["bear_events_5"] = ev[bear].sum(axis=1).rolling(5, min_periods=1).sum() if bear else 0.0
    return feats.replace([np.inf, -np.inf], np.nan)


def build_dataset(
    ohlcv: pd.DataFrame,
    horizon: int = 1,
    external_events: pd.DataFrame | None = None,
    fast_ma: int = 20,
    slow_ma: int = 50,
) -> Dataset:
    if horizon < 1:
        raise ValueError("horizon must be >= 1")
    ind = add_indicators(ohlcv, fast_ma=fast_ma, slow_ma=slow_ma)
    events = detect_events(ind)
    if external_events is not None and not external_events.empty:
        events = events.join(external_events.reindex(events.index, fill_value=False).astype(bool))
    feats = build_features(ind, events)
    X = feats.dropna()
    fwd = forward_returns(ind["close"], horizon).reindex(X.index)
    return Dataset(frame=ind, events=events, X=X, fwd_return=fwd, horizon=horizon)
