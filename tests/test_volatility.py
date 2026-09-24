import numpy as np

from crypto_bot.volatility import evaluate_volatility, forecast_range

from .conftest import make_ohlcv


def test_range_coverage_is_calibrated_on_gaussian_returns():
    rng = np.random.default_rng(3)
    df = make_ohlcv(rng.normal(0, 0.02, 1200))
    result = evaluate_volatility(df["close"], horizon=1)
    assert abs(result["coverage_68"] - 0.68) < 0.05
    assert abs(result["coverage_95"] - 0.95) < 0.03


def test_forecast_range_is_ordered(random_walk):
    out = forecast_range(random_walk["close"], horizon=3)
    last = random_walk["close"].iloc[-1]
    assert out["range_95"][0] < out["range_68"][0] < last < out["range_68"][1] < out["range_95"][1]
    assert out["sigma"] > 0
