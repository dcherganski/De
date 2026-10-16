"""Market features from closed candles and the latest quote. Only past data is used.

Jev reads text better than numbers (docs.typesafe.ai/model-jaggedness/jev-1.13: "Jev is not a
calculator"; dates and numeric comparisons are unreliable), so every number is paired with a plain
label computed here in code, and every threshold stays in code. Labels are in English because that
is where Jev is most accurate (docs.typesafe.ai/models, "Language support").
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from ..market_data.types import CandleBar, Quote


@dataclass
class Features:
    values: dict
    labels: dict

    def as_state(self) -> dict:
        return {k: {"value": self.values[k], "reading": self.labels.get(k, "")} for k in self.values}


def _ret(closes: list[float], n: int) -> float | None:
    if len(closes) <= n or closes[-1 - n] == 0:
        return None
    return closes[-1] / closes[-1 - n] - 1.0


def _rsi(closes: list[float], period: int = 14) -> float | None:
    if len(closes) <= period:
        return None
    gains, losses = [], []
    for a, b in zip(closes[:-1], closes[1:], strict=True):
        d = b - a
        gains.append(max(d, 0.0))
        losses.append(max(-d, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for g, lo in zip(gains[period:], losses[period:], strict=True):  # Wilder smoothing
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + lo) / period
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def _ema(values: list[float], span: int) -> float | None:
    if len(values) < span:
        return None
    alpha = 2.0 / (span + 1)
    e = sum(values[:span]) / span
    for v in values[span:]:
        e = alpha * v + (1 - alpha) * e
    return e


def _std(xs: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def move_label(r: float | None, typical: float | None) -> str:
    if r is None:
        return "not enough history"
    size = abs(r) / typical if typical else None
    word = "up" if r > 0 else "down" if r < 0 else "unchanged"
    if size is None:
        strength = ""
    elif size < 0.5:
        strength = " (small)"
    elif size < 1.5:
        strength = " (typical)"
    elif size < 3:
        strength = " (large)"
    else:
        strength = " (very large)"
    return f"{word} {abs(r) * 100:.2f}%{strength}"


def compute_features(candles: list[CandleBar], quote: Quote | None) -> Features:
    """`candles` are closed 5-minute bars, oldest first."""
    closes = [float(c.close) for c in candles]
    vols = [float(c.volume) for c in candles]
    rets = [b / a - 1.0 for a, b in zip(closes[:-1], closes[1:], strict=True) if a]
    vol_1h = _std(rets[-12:])
    typical_30m = vol_1h * math.sqrt(6) if vol_1h else None
    values: dict = {}
    labels: dict = {}

    for name, n, scale in (("return_5m", 1, 1), ("return_30m", 6, 6), ("return_1h", 12, 12), ("return_4h", 48, 48)):
        r = _ret(closes, n)
        values[name] = round(r * 100, 3) if r is not None else None
        labels[name] = move_label(r, vol_1h * math.sqrt(scale) if vol_1h else None)

    values["volatility_1h_per_5m_pct"] = round(vol_1h * 100, 4) if vol_1h else None
    labels["volatility_1h_per_5m_pct"] = (
        "no data" if vol_1h is None else "low" if vol_1h < 0.0008 else "moderate" if vol_1h < 0.002 else "high"
    )
    values["typical_30m_move_pct"] = round(typical_30m * 100, 3) if typical_30m else None
    labels["typical_30m_move_pct"] = "typical size of a 30-minute move at the current volatility"

    rsi = _rsi(closes)
    values["rsi_14_on_5m"] = round(rsi, 1) if rsi is not None else None
    labels["rsi_14_on_5m"] = (
        "no data" if rsi is None else "overbought" if rsi >= 70 else "oversold" if rsi <= 30 else "neutral"
    )

    ema20, ema50 = _ema(closes, 20), _ema(closes, 50)
    if ema20 and ema50 and closes:
        values["price_vs_ema20_pct"] = round((closes[-1] / ema20 - 1) * 100, 3)
        values["ema20_vs_ema50_pct"] = round((ema20 / ema50 - 1) * 100, 3)
        labels["price_vs_ema20_pct"] = "above the short-term average" if closes[-1] > ema20 else "below the short-term average"
        labels["ema20_vs_ema50_pct"] = "short-term uptrend" if ema20 > ema50 else "short-term downtrend"
    else:
        values["price_vs_ema20_pct"] = values["ema20_vs_ema50_pct"] = None
        labels["price_vs_ema20_pct"] = labels["ema20_vs_ema50_pct"] = "not enough history"

    if len(vols) >= 13 and sum(vols[-13:-1]) > 0:
        rel = vols[-1] / (sum(vols[-13:-1]) / 12)
        values["volume_last_vs_1h_avg"] = round(rel, 2)
        labels["volume_last_vs_1h_avg"] = "very high volume" if rel > 3 else "high volume" if rel > 1.5 else "low volume" if rel < 0.5 else "normal volume"
    else:
        values["volume_last_vs_1h_avg"] = None
        labels["volume_last_vs_1h_avg"] = "not enough history"

    if quote is not None:
        values["spread_pct"] = round(float(quote.spread_pct), 4)
        labels["spread_pct"] = "tight spread" if quote.spread_pct < Decimal("0.05") else "wide spread"
    values["candles_used"] = len(candles)
    labels["candles_used"] = "closed 5-minute candles"
    return Features(values, labels)
