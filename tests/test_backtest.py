import numpy as np
import pandas as pd

from crypto_bot.backtest import edge_test, performance_weights, run_backtest, simulate_strategy, walk_forward
from crypto_bot.features import build_dataset
from crypto_bot.models import EventStudyModel, MarkovModel
from tests.conftest import make_ohlcv


def light_models():
    return [EventStudyModel(), MarkovModel()]


def test_walk_forward_never_trains_on_the_future(random_walk):
    ds = build_dataset(random_walk, horizon=2)
    preds = walk_forward(ds, models_factory=light_models, min_train=100, step=10)
    first_pos = ds.X.index.get_loc(preds.index[0])
    # Row j's 2-bar outcome is known at bar j + 2, so bar 101 is the first with 100 known rows.
    assert first_pos == 100 + 2 - 1
    assert preds["train_size"].is_monotonic_increasing
    # Each test bar is trained with at most (its position - horizon + 1) rows.
    positions = np.array([ds.X.index.get_loc(t) for t in preds.index])
    assert (preds["train_size"].to_numpy() <= positions - 2 + 1).all()
    assert preds["actual_return"].notna().all()


def test_simulate_strategy_math():
    idx = pd.date_range("2024-01-01", periods=4, freq="D", tz="UTC")
    prob = pd.Series([0.9, 0.9, 0.1, 0.5], index=idx)
    ret = pd.Series([0.10, -0.05, 0.20, 0.01], index=idx)
    equity, stats = simulate_strategy(prob, ret, threshold=0.02, fee=0.01, long_only=True)
    # long, long, flat, flat: (1+0.10-0.01) * (1-0.05) then a 0.01 fee to exit
    expected = (1.09) * (0.95) * (1 - 0.01)
    assert np.isclose(equity["strategy"].iloc[-1], expected)
    assert stats["trades"] == 2
    assert np.isclose(stats["buy_and_hold_return"], 1.10 * 0.95 * 1.20 * 1.01 - 1)


def test_short_side_when_allowed():
    idx = pd.date_range("2024-01-01", periods=2, freq="D", tz="UTC")
    equity, _ = simulate_strategy(pd.Series([0.1, 0.1], index=idx), pd.Series([-0.1, -0.1], index=idx), fee=0.0, long_only=False)
    assert np.isclose(equity["strategy"].iloc[-1], 1.1 * 1.1)


def test_performance_weights():
    metrics = pd.DataFrame({"brier": [0.24, 0.26, 0.25, 0.20]}, index=["a", "b", "c", "ensemble"])
    w = performance_weights(metrics)
    assert set(w) == {"a", "b", "c"}
    assert np.isclose(sum(w.values()), 1.0, atol=1e-3)
    assert w["a"] > w["b"]
    bad = pd.DataFrame({"brier": [0.26, 0.27]}, index=["a", "b"])
    assert performance_weights(bad) == {"a": 0.5, "b": 0.5}


def test_run_backtest_end_to_end(random_walk):
    res = run_backtest(build_dataset(random_walk), min_train=120, step=20, models_factory=light_models)
    assert {"events", "markov", "ensemble"} <= set(res.metrics.index)
    assert res.metrics["accuracy"].between(0, 1).all()
    assert len(res.equity) == res.predictions["next_return"].notna().sum()
    assert res.predictions["base_rate"].between(0, 1).all()
    assert {"auc_p", "brier_skill_base"} <= set(res.metrics.columns)


def test_edge_test_hurdles():
    def metrics(n=499, auc=0.56, auc_p=0.005, skill_base=0.01):
        return pd.DataFrame({"n": [n], "auc": [auc], "auc_p": [auc_p], "brier_skill_base": [skill_base]}, index=["ensemble"])

    ok, reason = edge_test(metrics())
    assert ok and "✗" not in reason
    # Each hurdle on its own is enough to fail, and the reason names the one that did.
    for bad in ({"n": 150}, {"auc_p": 0.02}, {"skill_base": -0.001}, {"auc_p": float("nan")}):
        ok, reason = edge_test(metrics(**bad))
        assert not ok and reason.count("✗") == 1


def test_edge_needs_a_real_pattern(random_walk):
    rng = np.random.default_rng(0)
    r = np.zeros(600)
    for t in range(1, len(r)):
        r[t] = 0.5 * r[t - 1] + rng.normal(0, 0.02)  # strong, learnable autocorrelation
    planted = run_backtest(build_dataset(make_ohlcv(r, seed=2)), min_train=120, step=25, models_factory=light_models)
    assert planted.has_edge and planted.metrics.loc["ensemble", "auc_p"] < 0.01
    noise = run_backtest(build_dataset(random_walk), min_train=120, step=25, models_factory=light_models)
    assert not noise.has_edge and "✗" in noise.edge_reason
