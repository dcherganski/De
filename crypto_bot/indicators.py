"""Technical indicators. Every function is causal: the value at bar t only uses bars <= t."""

from __future__ import annotations

import numpy as np
import pandas as pd


def log_returns(close: pd.Series) -> pd.Series:
    return np.log(close / close.shift(1))


def ema(series: pd.Series, span: int) -> pd.Series:
    return series.ewm(span=span, adjust=False, min_periods=span).mean()


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's Relative Strength Index (0..100)."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    # Only gains in the window -> RSI is 100 by definition.
    out = out.where(~((avg_loss == 0) & (avg_gain > 0)), 100.0)
    return out


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False, min_periods=signal).mean()
    return pd.DataFrame({"macd": line, "macd_signal": sig, "macd_hist": line - sig})


def bollinger(close: pd.Series, window: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = close.rolling(window).mean()
    std = close.rolling(window).std(ddof=0)
    upper = mid + k * std
    lower = mid - k * std
    width = (upper - lower).replace(0.0, np.nan)
    return pd.DataFrame(
        {
            "bb_mid": mid,
            "bb_upper": upper,
            "bb_lower": lower,
            "bb_pct_b": (close - lower) / width,
            "bb_bandwidth": width / mid,
        }
    )


def atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    prev_close = df["close"].shift(1)
    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - prev_close).abs(),
            (df["low"] - prev_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    return true_range.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def zscore(series: pd.Series, window: int) -> pd.Series:
    mean = series.rolling(window).mean()
    std = series.rolling(window).std(ddof=0).replace(0.0, np.nan)
    return (series - mean) / std


def add_indicators(
    df: pd.DataFrame,
    fast_ma: int = 20,
    slow_ma: int = 50,
) -> pd.DataFrame:
    """Return a copy of an OHLCV frame with the indicator columns used by the bot."""
    out = df.copy()
    close = out["close"]
    out["log_ret"] = log_returns(close)
    out["ema_fast"] = ema(close, fast_ma)
    out["ema_slow"] = ema(close, slow_ma)
    out["rsi"] = rsi(close)
    out = out.join(macd(close))
    out = out.join(bollinger(close))
    out["atr"] = atr(out)
    out["atr_pct"] = out["atr"] / close
    out["vol_z"] = zscore(np.log(out["volume"].replace(0.0, np.nan)), 20)
    out["ret_z"] = out["log_ret"] / out["log_ret"].rolling(30).std(ddof=0).shift(1)
    out["high_n"] = out["high"].rolling(20).max().shift(1)
    out["low_n"] = out["low"].rolling(20).min().shift(1)
    return out
