import numpy as np
import pandas as pd

from crypto_bot.features import build_dataset


def test_no_lookahead_in_features(random_walk):
    full = build_dataset(random_walk)
    for k in (200, 300, 400):
        partial = build_dataset(random_walk.iloc[:k])
        ts = partial.X.index[-1]
        pd.testing.assert_series_equal(partial.X.iloc[-1], full.X.loc[ts], check_names=False)


def test_forward_return_alignment(random_walk):
    ds = build_dataset(random_walk, horizon=3)
    close = random_walk["close"]
    ts = ds.X.index[10]
    pos = close.index.get_loc(ts)
    assert np.isclose(ds.fwd_return.loc[ts], np.log(close.iloc[pos + 3] / close.iloc[pos]))
    assert ds.fwd_return.iloc[-3:].isna().all()
    assert ds.y.dropna().isin([0.0, 1.0]).all()


def test_features_have_no_missing_values(random_walk):
    ds = build_dataset(random_walk)
    assert not ds.X.isna().any().any()
    assert {"ev_golden_cross", "rsi", "vol_z", "dow_sin"} <= set(ds.X.columns)
