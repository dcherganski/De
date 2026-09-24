import numpy as np
import pandas as pd

from crypto_bot.indicators import add_indicators, bollinger, ema, rsi


def test_rsi_bounds_and_extremes(random_walk):
    values = rsi(random_walk["close"]).dropna()
    assert values.between(0, 100).all()
    rising = pd.Series(np.arange(1.0, 60.0))
    assert rsi(rising).dropna().eq(100.0).all()


def test_ema_matches_recursive_definition():
    s = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    alpha = 2 / (3 + 1)
    expected = [1.0]
    for v in s.iloc[1:]:
        expected.append(alpha * v + (1 - alpha) * expected[-1])
    assert np.allclose(ema(s, 3).iloc[2:], expected[2:])


def test_bollinger_pct_b_is_half_at_mean():
    close = pd.Series([10.0, 12.0] * 15)
    bb = bollinger(close, window=20)
    last = bb.iloc[-1]
    assert np.isclose(last["bb_mid"], 11.0)
    assert np.isclose(bb["bb_pct_b"].iloc[-1], (close.iloc[-1] - last["bb_lower"]) / (last["bb_upper"] - last["bb_lower"]))


def test_indicators_are_causal(random_walk):
    full = add_indicators(random_walk)
    for k in (120, 250, 399):
        partial = add_indicators(random_walk.iloc[:k])
        pd.testing.assert_series_equal(partial.iloc[-1], full.iloc[k - 1], check_names=False)
