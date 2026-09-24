"""The prediction bot: data -> events -> models -> backtest -> forecast for the next candle."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime

import numpy as np
import pandas as pd

from .backtest import BacktestResult, run_backtest
from .data import CoinbaseClient, drop_incomplete, granularity_seconds, load_csv, update_cache
from .events import (
    active_events,
    event_label,
    event_study,
    load_external_events,
    recent_events,
    upcoming_events,
)
from .features import Dataset, build_dataset
from .models import MODEL_LABELS, AnalogModel, EnsembleModel, default_models
from .volatility import evaluate_volatility, forecast_range


@dataclass
class BotConfig:
    product: str = "BTC-USD"
    granularity: str = "1d"
    horizon: int = 1
    csv: str | None = None  # read candles from this file instead of the API
    cache_dir: str = "data"
    days: int = 730  # history to download on first run
    events_file: str | None = None  # optional CSV of external events (date,name)
    min_train: int = 150
    step: int = 10  # walk-forward re-training interval (bars)
    threshold: float = 0.02  # probability margin around 50% needed for a BUY/SELL signal
    fee: float = 0.001
    long_only: bool = True
    backtest: bool = True


@dataclass
class Prediction:
    product: str
    granularity: str
    horizon: int
    as_of: str  # open time of the last closed candle used
    target_close_time: str  # when the predicted candle closes
    last_close: float
    prob_up: float
    exp_return: float
    expected_price: float
    sigma: float
    range_68: tuple[float, float]
    range_95: tuple[float, float]
    signal: str  # BUY / SELL / HOLD
    signal_reason: str
    has_edge: bool | None
    models: dict[str, dict] = field(default_factory=dict)
    active_events: list[dict] = field(default_factory=list)
    recent_events: list[dict] = field(default_factory=list)
    analogs: list[dict] = field(default_factory=list)
    upcoming_events: list[dict] = field(default_factory=list)  # external events inside the forecast window

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BotResult:
    config: BotConfig
    prediction: Prediction
    dataset: Dataset
    event_table: pd.DataFrame
    backtest: BacktestResult | None
    volatility: dict


def _iso(ts) -> str:
    return pd.Timestamp(ts).isoformat()


class PredictionBot:
    def __init__(self, config: BotConfig | None = None, client: CoinbaseClient | None = None):
        self.config = config or BotConfig()
        self.client = client

    def load_candles(self, now: datetime | None = None) -> pd.DataFrame:
        cfg = self.config
        if cfg.csv:
            df = load_csv(cfg.csv)
        else:
            df = update_cache(cfg.product, cfg.granularity, cfg.cache_dir, cfg.days, client=self.client)
        return drop_incomplete(df, cfg.granularity, now=now)

    def build(self, candles: pd.DataFrame) -> Dataset:
        external = load_external_events(self.config.events_file, candles.index) if self.config.events_file else None
        return build_dataset(candles, horizon=self.config.horizon, external_events=external)

    def run(self, candles: pd.DataFrame | None = None, now: datetime | None = None) -> BotResult:
        cfg = self.config
        candles = candles if candles is not None else self.load_candles(now=now)
        if len(candles) < cfg.min_train + 80:
            raise ValueError(
                f"need at least {cfg.min_train + 80} closed candles, got {len(candles)}; "
                "increase --days or lower --min-train"
            )
        ds = self.build(candles)
        bt = None
        weights = None
        if cfg.backtest:
            bt = run_backtest(
                ds,
                min_train=cfg.min_train,
                step=cfg.step,
                threshold=cfg.threshold,
                fee=cfg.fee,
                long_only=cfg.long_only,
            )
            weights = bt.weights

        # Final models: train on every bar whose outcome is known, predict the latest bar.
        train_idx = ds.labeled
        ensemble = EnsembleModel(default_models(), weights=weights)
        ensemble.fit(ds.X.loc[train_idx], ds.fwd_return.loc[train_idx])
        latest = ds.X.iloc[[-1]]
        outputs = ensemble.predict_all(latest)
        prob = float(outputs["ensemble"]["prob_up"].iloc[0])
        exp_ret = float(outputs["ensemble"]["exp_return"].iloc[0])

        table = event_study(ds.frame["close"], ds.events, cfg.horizon)
        vol_range = forecast_range(ds.frame["close"], cfg.horizon, exp_return=exp_ret)
        vol_eval = evaluate_volatility(ds.frame["close"], cfg.horizon)

        signal, reason, has_edge = self._signal(prob, bt)
        last_ts = ds.frame.index[-1]
        bar = pd.Timedelta(seconds=granularity_seconds(cfg.granularity))
        last_close = float(ds.frame["close"].iloc[-1])

        analog_model = next((m for m in ensemble.models if isinstance(m, AnalogModel)), None)
        if analog_model is None:
            analog_model = AnalogModel().fit(ds.X.loc[train_idx], ds.fwd_return.loc[train_idx])
        analogs = analog_model.analogs(latest).head(5)

        prediction = Prediction(
            product=cfg.product,
            granularity=cfg.granularity,
            horizon=cfg.horizon,
            as_of=_iso(last_ts),
            target_close_time=_iso(last_ts + bar * (cfg.horizon + 1)),
            last_close=last_close,
            prob_up=prob,
            exp_return=exp_ret,
            expected_price=last_close * float(np.exp(exp_ret)),
            sigma=vol_range["sigma"],
            range_68=vol_range["range_68"],
            range_95=vol_range["range_95"],
            signal=signal,
            signal_reason=reason,
            has_edge=has_edge,
            models={
                name: {
                    "label": MODEL_LABELS.get(name, name),
                    "prob_up": float(out["prob_up"].iloc[0]),
                    "exp_return": float(out["exp_return"].iloc[0]),
                    "weight": float(ensemble.weights.get(name, 0.0)) if name != "ensemble" else 1.0,
                }
                for name, out in outputs.items()
            },
            active_events=[
                {
                    "event": name,
                    "label": event_label(name),
                    "count": int(table.loc[name, "count"]),
                    "hit_rate": _nan_to_none(table.loc[name, "hit_rate"]),
                    "mean_return": _nan_to_none(table.loc[name, "mean_return"]),
                }
                for name in active_events(ds.events)
            ],
            recent_events=[
                {"time": _iso(ts), "event": name, "label": event_label(name)}
                for ts, name in recent_events(ds.events, bars=10)
            ],
            analogs=[
                {"time": _iso(ts), "distance": float(row.distance), "fwd_return": float(row.fwd_return)}
                for ts, row in analogs.iterrows()
            ],
            upcoming_events=upcoming_events(cfg.events_file, last_ts + bar, last_ts + bar * (cfg.horizon + 1))
            if cfg.events_file
            else [],
        )
        return BotResult(cfg, prediction, ds, table, bt, vol_eval)

    def _signal(self, prob: float, bt: BacktestResult | None) -> tuple[str, str, bool | None]:
        thr = self.config.threshold
        has_edge = None
        if bt is not None:
            ens = bt.metrics.loc["ensemble"]
            has_edge = bool(ens["brier_skill"] > 0 and ens["auc"] > 0.5)
            if not has_edge:
                return (
                    "HOLD",
                    "Моделите нямат доказано предимство в walk-forward теста (Brier skill ≤ 0 или AUC ≤ 0.5) — "
                    "прогнозата за посока е само информативна.",
                    False,
                )
        if prob > 0.5 + thr:
            return "BUY", f"Вероятност за ръст {prob:.1%} > {0.5 + thr:.0%}.", has_edge
        if prob < 0.5 - thr:
            action = "SELL"
            note = " (long-only режим: излез от позиция)" if self.config.long_only else ""
            return action, f"Вероятност за ръст {prob:.1%} < {0.5 - thr:.0%}{note}.", has_edge
        return "HOLD", f"Вероятността {prob:.1%} е в неутралната зона 50% ± {thr:.0%}.", has_edge


def _nan_to_none(x):
    x = float(x)
    return None if np.isnan(x) else x

