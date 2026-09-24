import numpy as np
import pandas as pd
import pytest


def make_ohlcv(returns: np.ndarray, start: str = "2024-01-01", freq: str = "D", seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    close = 100.0 * np.exp(np.cumsum(returns))
    open_ = np.r_[close[0], close[:-1]]
    wick = np.abs(rng.normal(0, 0.004, len(close)))
    index = pd.date_range(start, periods=len(close), freq=freq, tz="UTC", name="time")
    return pd.DataFrame(
        {
            "open": open_,
            "high": np.maximum(open_, close) * (1 + wick),
            "low": np.minimum(open_, close) * (1 - wick),
            "close": close,
            "volume": rng.uniform(100, 200, len(close)),
        },
        index=index,
    )


@pytest.fixture
def random_walk() -> pd.DataFrame:
    rng = np.random.default_rng(42)
    return make_ohlcv(rng.normal(0.0005, 0.02, 400), seed=1)


@pytest.fixture
def momentum() -> pd.DataFrame:
    """AR(1) returns with positive autocorrelation: a pattern the bot should find."""
    rng = np.random.default_rng(0)
    r = np.zeros(420)
    for t in range(1, len(r)):
        r[t] = 0.4 * r[t - 1] + rng.normal(0, 0.02)
    return make_ohlcv(r, seed=2)
