"""Walk-forward backtesting: the only honest way to judge a market predictor.

At every test bar the models are trained exclusively on bars whose outcome was already
known at that moment, then asked to predict the future. Nothing from the future leaks in.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

import numpy as np
import pandas as pd
from scipy.stats import mannwhitneyu
from sklearn.metrics import roc_auc_score

from .features import Dataset, _bar_seconds
from .models import BaseModel, EnsembleModel, default_models

# The edge test: fewer predictions than this, or a p-value above this, is not evidence.
EDGE_MIN_PREDICTIONS = 200
EDGE_ALPHA = 0.01


@dataclass
class BacktestResult:
    predictions: pd.DataFrame  # per test bar: <model>_prob, <model>_ret, actual_return, next_return
    metrics: pd.DataFrame  # per model scores
    equity: pd.DataFrame  # strategy vs buy & hold equity curves
    strategy: dict  # summary of the simulated strategy
    weights: dict[str, float] = field(default_factory=dict)  # suggested ensemble weights
    has_edge: bool = False  # did the ensemble pass `edge_test`?
    edge_reason: str = ""  # the three hurdles of `edge_test` with their outcome


def walk_forward(
    ds: Dataset,
    models_factory: Callable[[], list[BaseModel]] = default_models,
    min_train: int = 100,
    step: int = 5,
) -> pd.DataFrame:
    X, fwd, h = ds.X, ds.fwd_return, ds.horizon
    pos = ds.frame.index.get_indexer(X.index)  # bar number of every feature row
    labeled = fwd.notna().to_numpy()
    close = ds.frame["close"]
    next_ret = (close.shift(-1) / close - 1.0).reindex(X.index)

    rows = []
    i = 0
    n = len(X)
    while i < n:
        train_mask = labeled & (pos + h <= pos[i])
        if train_mask.sum() < min_train:
            i += 1
            continue
        block = slice(i, min(i + step, n))
        test_idx = X.index[block][labeled[block]]
        if len(test_idx):
            ens = EnsembleModel(models_factory())
            ens.fit(X[train_mask], fwd[train_mask])
            outputs = ens.predict_all(X.loc[test_idx])
            frame = pd.DataFrame(index=test_idx)
            for name, out in outputs.items():
                frame[f"{name}_prob"] = out["prob_up"]
                frame[f"{name}_ret"] = out["exp_return"]
            frame["train_size"] = int(train_mask.sum())
            # Up-frequency known at the time: the benchmark a forecast has to beat.
            frame["base_rate"] = float((fwd[train_mask] > 0).mean())
            rows.append(frame)
        i = block.stop
    if not rows:
        raise ValueError(
            f"not enough history for a walk-forward test: need > {min_train} labeled rows, have {int(labeled.sum())}"
        )
    preds = pd.concat(rows)
    preds["actual_return"] = fwd.reindex(preds.index)
    preds["next_return"] = next_ret.reindex(preds.index)
    return preds


def _auc_p_value(prob: pd.Series, up: pd.Series) -> float:
    """One-sided Mann-Whitney test of AUC > 0.5: do up bars get higher probabilities than
    down bars more often than chance would give?"""
    pos, neg = prob[up > 0.5], prob[up <= 0.5]
    if len(pos) < 2 or len(neg) < 2:
        return float("nan")
    return float(mannwhitneyu(pos, neg, alternative="greater").pvalue)


def score_predictions(preds: pd.DataFrame, confident_margin: float = 0.05) -> pd.DataFrame:
    actual = preds["actual_return"]
    up = (actual > 0).astype(float)
    # A coin flip is too easy a benchmark in a trending market: always predicting the
    # up-frequency known at the time already scores below 0.25 with zero skill.
    base = preds["base_rate"] if "base_rate" in preds else pd.Series(0.5, index=preds.index)
    brier_base = float(((base - up) ** 2).mean())
    names = [c[: -len("_prob")] for c in preds.columns if c.endswith("_prob")]
    rows = []
    for name in names:
        p = preds[f"{name}_prob"].clip(1e-6, 1 - 1e-6)
        r = preds[f"{name}_ret"]
        brier = float(((p - up) ** 2).mean())
        confident = (p - 0.5).abs() >= confident_margin
        rows.append(
            {
                "model": name,
                "n": int(len(p)),
                "accuracy": float(((p > 0.5) == (up > 0.5)).mean()),
                "brier": brier,
                "brier_skill": 1.0 - brier / 0.25,  # vs. a 50/50 coin flip
                "brier_skill_base": 1.0 - brier / brier_base if brier_base > 0 else float("nan"),
                "log_loss": float(-(up * np.log(p) + (1 - up) * np.log(1 - p)).mean()),
                "auc": float(roc_auc_score(up, p)) if up.nunique() > 1 else float("nan"),
                "auc_p": _auc_p_value(p, up),
                "confident_n": int(confident.sum()),
                "confident_accuracy": float(((p[confident] > 0.5) == (up[confident] > 0.5)).mean())
                if confident.any()
                else float("nan"),
                "return_ic": float(r.corr(actual, method="spearman")),
            }
        )
    table = pd.DataFrame(rows).set_index("model")
    table.attrs["always_up_accuracy"] = float(up.mean())
    return table


def edge_test(
    metrics: pd.DataFrame, min_predictions: int = EDGE_MIN_PREDICTIONS, alpha: float = EDGE_ALPHA
) -> tuple[bool, str]:
    """Does the ensemble have a real out-of-sample edge, or just a lucky-looking number?

    Three hurdles, all required: enough predictions for a test to mean anything, an AUC
    above 0.5 that a one-sided Mann-Whitney test does not attribute to chance, and a Brier
    score better than the base rate known at the time. Returns the verdict and a
    one-line account of each hurdle, so a reader can see which one failed.
    """
    ens = metrics.loc["ensemble"]
    n = int(ens["n"])
    p = float(ens["auc_p"])
    skill = float(ens["brier_skill_base"])
    checks = [
        (n >= min_predictions, f"{n} прогнози извън извадката (нужни са поне {min_predictions})"),
        (
            np.isfinite(p) and p < alpha,
            f"AUC {ens['auc']:.3f} с p = {p:.3f} (нужно е p < {alpha:g})" if np.isfinite(p) else "AUC не може да се изчисли",
        ),
        (skill > 0, f"Brier skill спрямо базовата честота {skill:+.3f} (нужно е > 0)"),
    ]
    return all(ok for ok, _ in checks), "; ".join(f"{text} {'✓' if ok else '✗'}" for ok, text in checks)


def performance_weights(metrics: pd.DataFrame) -> dict[str, float]:
    """Ensemble weights from out-of-sample skill, half-blended with equal weights so a
    lucky streak on a short history cannot hand one model the whole vote."""
    members = metrics.drop(index="ensemble", errors="ignore")
    skill = (0.25 - members["brier"]).clip(lower=0.0)
    equal = pd.Series(1.0 / len(members), index=members.index)
    if skill.sum() <= 0:
        return equal.round(4).to_dict()
    return (0.5 * equal + 0.5 * skill / skill.sum()).round(4).to_dict()


def simulate_strategy(
    prob: pd.Series,
    next_return: pd.Series,
    threshold: float = 0.02,
    fee: float = 0.001,
    long_only: bool = True,
    periods_per_year: float = 365.0,
) -> tuple[pd.DataFrame, dict]:
    """Follow the signal bar by bar: long when prob > 0.5+threshold, flat/short when below
    0.5-threshold. `fee` is charged per unit of position change (0.001 = 0.1%)."""
    short = 0.0 if long_only else -1.0
    signal = pd.Series(np.where(prob > 0.5 + threshold, 1.0, np.where(prob < 0.5 - threshold, short, 0.0)), index=prob.index)
    turnover = signal.diff().abs().fillna(signal.abs())
    strat = signal * next_return - turnover * fee
    equity = pd.DataFrame(
        {
            "strategy": (1.0 + strat).cumprod(),
            "buy_and_hold": (1.0 + next_return).cumprod(),
            "signal": signal,
        }
    )
    sd = strat.std(ddof=0)
    in_market = signal != 0
    peak = equity["strategy"].cummax()
    stats = {
        "total_return": float(equity["strategy"].iloc[-1] - 1.0),
        "buy_and_hold_return": float(equity["buy_and_hold"].iloc[-1] - 1.0),
        "sharpe": float(strat.mean() / sd * np.sqrt(periods_per_year)) if sd > 0 else 0.0,
        "max_drawdown": float((equity["strategy"] / peak - 1.0).min()),
        "trades": int((turnover > 0).sum()),
        "exposure": float(in_market.mean()),
        "win_rate": float((strat[in_market] > 0).mean()) if in_market.any() else float("nan"),
        "threshold": threshold,
        "fee": fee,
        "long_only": long_only,
    }
    return equity, stats


def run_backtest(
    ds: Dataset,
    min_train: int = 100,
    step: int = 5,
    threshold: float = 0.02,
    fee: float = 0.001,
    long_only: bool = True,
    models_factory: Callable[[], list[BaseModel]] = default_models,
) -> BacktestResult:
    preds = walk_forward(ds, models_factory=models_factory, min_train=min_train, step=step)
    metrics = score_predictions(preds)
    periods = 365.0 * 86400.0 / _bar_seconds(ds.frame.index)
    valid = preds["next_return"].notna()
    equity, stats = simulate_strategy(
        preds.loc[valid, "ensemble_prob"],
        preds.loc[valid, "next_return"],
        threshold=threshold,
        fee=fee,
        long_only=long_only,
        periods_per_year=periods,
    )
    has_edge, edge_reason = edge_test(metrics)
    return BacktestResult(
        predictions=preds,
        metrics=metrics,
        equity=equity,
        strategy=stats,
        weights=performance_weights(metrics),
        has_edge=has_edge,
        edge_reason=edge_reason,
    )
