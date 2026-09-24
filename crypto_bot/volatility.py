"""Volatility forecasting and price ranges.

Direction is close to a coin flip in liquid crypto markets, but the *size* of moves is
persistent (volatility clusters), so a range forecast is where past data carries the most
information. We use the RiskMetrics EWMA estimator and calibrate the band width with the
empirical distribution of past standardized returns, which accounts for fat tails.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Quantiles that bound the central 68% and 95% of outcomes.
BANDS = {"68": (0.16, 0.84), "95": (0.025, 0.975)}


def ewma_sigma(log_ret: pd.Series, lam: float = 0.94) -> pd.Series:
    """One-bar-ahead volatility forecast: the value at t uses returns up to and including t."""
    var = (log_ret**2).ewm(alpha=1.0 - lam, adjust=False, min_periods=20).mean()
    return np.sqrt(var)


def standardized_returns(log_ret: pd.Series, horizon: int = 1, lam: float = 0.94) -> pd.Series:
    """Realised h-bar return divided by the sigma that was forecast before it happened."""
    sigma = ewma_sigma(log_ret, lam) * np.sqrt(horizon)
    fwd = log_ret.rolling(horizon).sum().shift(-horizon)
    return (fwd / sigma).replace([np.inf, -np.inf], np.nan)


def band_multipliers(z: pd.Series) -> dict[str, tuple[float, float]]:
    """Empirical quantiles of standardized returns (fall back to normal if history is short)."""
    z = z.dropna()
    if len(z) < 50:
        return {"68": (-1.0, 1.0), "95": (-1.96, 1.96)}
    return {name: (float(z.quantile(lo)), float(z.quantile(hi))) for name, (lo, hi) in BANDS.items()}


def forecast_range(
    close: pd.Series, horizon: int = 1, exp_return: float = 0.0, lam: float = 0.94
) -> dict:
    log_ret = np.log(close / close.shift(1))
    sigma_1 = float(ewma_sigma(log_ret, lam).iloc[-1])
    sigma_h = sigma_1 * np.sqrt(horizon)
    mult = band_multipliers(standardized_returns(log_ret, horizon, lam))
    last = float(close.iloc[-1])
    out = {"sigma": float(sigma_h), "sigma_1bar": sigma_1}
    for name, (lo, hi) in mult.items():
        out[f"range_{name}"] = (
            float(last * np.exp(exp_return + lo * sigma_h)),
            float(last * np.exp(exp_return + hi * sigma_h)),
        )
    return out


def evaluate_volatility(close: pd.Series, horizon: int = 1, lam: float = 0.94, min_history: int = 100) -> dict:
    """Walk-forward check of the range forecast: how often did the price land inside the
    band, and does forecast sigma track the size of the realised move?"""
    log_ret = np.log(close / close.shift(1))
    sigma = ewma_sigma(log_ret, lam) * np.sqrt(horizon)
    z = standardized_returns(log_ret, horizon, lam)
    fwd = log_ret.rolling(horizon).sum().shift(-horizon)
    hits = {name: [] for name in BANDS}
    for i in range(min_history, len(close)):
        if np.isnan(fwd.iloc[i]) or np.isnan(sigma.iloc[i]):
            continue
        # Only standardized returns whose outcome was known at bar i may calibrate the band.
        mult = band_multipliers(z.iloc[: max(i - horizon + 1, 0)])
        for name, (lo, hi) in mult.items():
            s = sigma.iloc[i]
            hits[name].append(lo * s <= fwd.iloc[i] <= hi * s)
    valid = fwd.notna() & sigma.notna()
    valid.iloc[:min_history] = False
    return {
        "n": len(hits["68"]),
        "coverage_68": float(np.mean(hits["68"])) if hits["68"] else float("nan"),
        "coverage_95": float(np.mean(hits["95"])) if hits["95"] else float("nan"),
        # Spearman correlation between forecast sigma and the realised absolute move.
        "sigma_vs_move_corr": float(sigma[valid].corr(fwd[valid].abs(), method="spearman")),
    }
