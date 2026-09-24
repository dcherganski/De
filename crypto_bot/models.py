"""Prediction models. Each one learns from past bars and outputs, for new bars:

* ``prob_up``    - probability that the price is higher ``horizon`` bars later
* ``exp_return`` - expected log return over the same horizon

All models share the tiny interface ``fit(X, fwd_return)`` / ``predict(X)`` so the
walk-forward backtest and the ensemble can treat them uniformly.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingClassifier, HistGradientBoostingRegressor
from sklearn.linear_model import LogisticRegression, Ridge
from sklearn.neighbors import NearestNeighbors
from sklearn.preprocessing import StandardScaler

MODEL_LABELS = {
    "events": "Статистика на минали събития",
    "analogs": "Исторически аналози (k-NN)",
    "markov": "Марковска верига на режимите",
    "logistic": "Логистична регресия",
    "gbm": "Gradient boosting",
    "ensemble": "Ансамбъл",
}

_EPS = 1e-6


def _logit(p):
    p = np.clip(p, _EPS, 1 - _EPS)
    return np.log(p / (1 - p))


def _sigmoid(x):
    return 1.0 / (1.0 + np.exp(-x))


def _output(index, prob, exp_ret) -> pd.DataFrame:
    return pd.DataFrame(
        {"prob_up": np.clip(np.asarray(prob, dtype=float), 0.0, 1.0), "exp_return": np.asarray(exp_ret, dtype=float)},
        index=index,
    )


class BaseModel:
    name = "base"

    def fit(self, X: pd.DataFrame, fwd_return: pd.Series) -> "BaseModel":
        raise NotImplementedError

    def predict(self, X: pd.DataFrame) -> pd.DataFrame:
        raise NotImplementedError

    def _fit_base(self, fwd_return: pd.Series) -> None:
        self.base_rate = float(np.clip((fwd_return > 0).mean(), 0.01, 0.99))
        self.base_mean = float(fwd_return.mean())


class EventStudyModel(BaseModel):
    """Bayesian event study: how often did the price rise after each active event?

    Hit rates are shrunk toward the unconditional base rate with a Beta prior, so rare
    events only move the forecast a little. Several simultaneous events are combined in
    log-odds space, damped by sqrt(k) because events tend to fire together.
    """

    name = "events"

    def __init__(self, prior_strength: float = 20.0):
        self.prior_strength = prior_strength

    def fit(self, X, fwd_return):
        self._fit_base(fwd_return)
        up = (fwd_return > 0).astype(float)
        a = self.prior_strength
        self.stats: dict[str, tuple[int, float, float]] = {}
        for col in (c for c in X.columns if c.startswith("ev_")):
            mask = X[col] > 0.5
            n = int(mask.sum())
            p = (up[mask].sum() + a * self.base_rate) / (n + a)
            m = (fwd_return[mask].sum() + a * self.base_mean) / (n + a)
            self.stats[col] = (n, float(p), float(m))
        return self

    def predict(self, X):
        base_logit = _logit(self.base_rate)
        logits = np.full(len(X), base_logit)
        rets = np.full(len(X), self.base_mean)
        active_count = np.zeros(len(X))
        shift_l = np.zeros(len(X))
        shift_r = np.zeros(len(X))
        for col, (_, p, m) in self.stats.items():
            if col not in X.columns:
                continue
            on = (X[col] > 0.5).to_numpy()
            active_count += on
            shift_l += on * (_logit(p) - base_logit)
            shift_r += on * (m - self.base_mean)
        damp = np.sqrt(np.maximum(active_count, 1.0))
        return _output(X.index, _sigmoid(logits + shift_l / damp), rets + shift_r / damp)


class AnalogModel(BaseModel):
    """Historical analogs: find the k past situations most similar to now (recent return
    path + indicator state) and look at what happened next."""

    name = "analogs"
    features = [
        "ret_1", "ret_lag_1", "ret_lag_2", "ret_lag_3", "ret_lag_4", "ret_7", "ret_14",
        "rsi", "bb_pct_b", "dist_ema_slow", "vol_ratio", "vol_z",
    ]

    def __init__(self, k: int = 25, prior_weight: float = 2.0):
        self.k = k
        self.prior_weight = prior_weight

    def _cols(self, X):
        return [c for c in self.features if c in X.columns]

    def fit(self, X, fwd_return):
        self._fit_base(fwd_return)
        self.cols = self._cols(X)
        self.scaler = StandardScaler().fit(X[self.cols])
        self.train_index = X.index
        self.train_returns = fwd_return.to_numpy(dtype=float)
        self.nn = NearestNeighbors(n_neighbors=min(self.k, len(X))).fit(self.scaler.transform(X[self.cols]))
        return self

    def neighbors(self, X) -> tuple[np.ndarray, np.ndarray]:
        dist, idx = self.nn.kneighbors(self.scaler.transform(X[self.cols]))
        return dist, idx

    def predict(self, X):
        dist, idx = self.neighbors(X)
        w = 1.0 / (dist + 1e-3)
        w = w / w.sum(axis=1, keepdims=True) * w.shape[1]
        r = self.train_returns[idx]
        up = (r > 0).astype(float)
        prob = ((w * up).sum(axis=1) + self.prior_weight * self.base_rate) / (w.sum(axis=1) + self.prior_weight)
        exp_ret = (w * r).sum(axis=1) / w.sum(axis=1)
        return _output(X.index, prob, exp_ret)

    def analogs(self, row: pd.DataFrame) -> pd.DataFrame:
        """The most similar past bars to a single row and what followed them."""
        dist, idx = self.neighbors(row)
        return pd.DataFrame(
            {"distance": dist[0], "fwd_return": self.train_returns[idx[0]]},
            index=self.train_index[idx[0]],
        )


class MarkovModel(BaseModel):
    """Second-order Markov chain over return regimes (down / flat / up)."""

    name = "markov"

    def __init__(self, smoothing: float = 5.0):
        self.smoothing = smoothing

    def _state(self, X):
        prev = np.digitize(X["ret_lag_1"].to_numpy(), self.edges)
        cur = np.digitize(X["ret_1"].to_numpy(), self.edges)
        return prev * 3 + cur

    def fit(self, X, fwd_return):
        self._fit_base(fwd_return)
        self.edges = np.quantile(X["ret_1"], [1 / 3, 2 / 3])
        states = self._state(X)
        up = (fwd_return > 0).to_numpy(dtype=float)
        ret = fwd_return.to_numpy(dtype=float)
        a = self.smoothing
        self.table = np.zeros((9, 3))  # n, prob_up, mean_return
        for s in range(9):
            mask = states == s
            n = mask.sum()
            self.table[s] = (
                n,
                (up[mask].sum() + a * self.base_rate) / (n + a),
                (ret[mask].sum() + a * self.base_mean) / (n + a),
            )
        return self

    def predict(self, X):
        s = self._state(X)
        return _output(X.index, self.table[s, 1], self.table[s, 2])


class LogisticModel(BaseModel):
    """L2-regularised logistic regression for direction + ridge regression for size."""

    name = "logistic"

    def __init__(self, C: float = 0.01, alpha: float = 200.0):
        self.C = C
        self.alpha = alpha

    def fit(self, X, fwd_return):
        self._fit_base(fwd_return)
        self.scaler = StandardScaler().fit(X)
        Z = self.scaler.transform(X)
        y = (fwd_return > 0).astype(int)
        self.clf = LogisticRegression(C=self.C, max_iter=2000).fit(Z, y) if y.nunique() > 1 else None
        self.reg = Ridge(alpha=self.alpha).fit(Z, fwd_return)
        return self

    def predict(self, X):
        Z = self.scaler.transform(X)
        prob = self.clf.predict_proba(Z)[:, 1] if self.clf is not None else np.full(len(X), self.base_rate)
        return _output(X.index, prob, self.reg.predict(Z))


class GradientBoostingModel(BaseModel):
    """Shallow, heavily regularised gradient-boosted trees (non-linear interactions)."""

    name = "gbm"

    def __init__(self, max_iter: int = 100, learning_rate: float = 0.03, max_depth: int = 2):
        self.params = dict(
            max_iter=max_iter,
            learning_rate=learning_rate,
            max_depth=max_depth,
            min_samples_leaf=30,
            l2_regularization=5.0,
            random_state=7,
        )

    def fit(self, X, fwd_return):
        self._fit_base(fwd_return)
        y = (fwd_return > 0).astype(int)
        self.clf = HistGradientBoostingClassifier(**self.params).fit(X, y) if y.nunique() > 1 else None
        self.reg = HistGradientBoostingRegressor(**self.params).fit(X, fwd_return)
        return self

    def predict(self, X):
        prob = self.clf.predict_proba(X)[:, 1] if self.clf is not None else np.full(len(X), self.base_rate)
        return _output(X.index, prob, self.reg.predict(X))


def default_models() -> list[BaseModel]:
    return [EventStudyModel(), AnalogModel(), MarkovModel(), LogisticModel(), GradientBoostingModel()]


class EnsembleModel:
    """Weighted average of the member models' probabilities and expected returns."""

    name = "ensemble"

    def __init__(self, models: list[BaseModel] | None = None, weights: dict[str, float] | None = None):
        self.models = models if models is not None else default_models()
        self.weights = weights or {m.name: 1.0 for m in self.models}

    def fit(self, X, fwd_return):
        for m in self.models:
            m.fit(X, fwd_return)
        return self

    def predict_all(self, X) -> dict[str, pd.DataFrame]:
        outputs = {m.name: m.predict(X) for m in self.models}
        outputs["ensemble"] = self.combine(outputs)
        return outputs

    def combine(self, outputs: dict[str, pd.DataFrame]) -> pd.DataFrame:
        names = [n for n in outputs if n != "ensemble"]
        w = np.array([max(self.weights.get(n, 0.0), 0.0) for n in names])
        if w.sum() <= 0:
            w = np.ones(len(names))
        w = w / w.sum()
        prob = sum(wi * outputs[n]["prob_up"].to_numpy() for wi, n in zip(w, names))
        ret = sum(wi * outputs[n]["exp_return"].to_numpy() for wi, n in zip(w, names))
        return _output(outputs[names[0]].index, prob, ret)

    def predict(self, X):
        return self.predict_all(X)["ensemble"]
