import numpy as np
import pytest

from crypto_bot.backtest import walk_forward, score_predictions
from crypto_bot.features import build_dataset
from crypto_bot.models import (
    AnalogModel,
    EnsembleModel,
    EventStudyModel,
    GradientBoostingModel,
    LogisticModel,
    MarkovModel,
    default_models,
)


@pytest.mark.parametrize("model_cls", [EventStudyModel, AnalogModel, MarkovModel, LogisticModel, GradientBoostingModel])
def test_models_output_valid_probabilities(random_walk, model_cls):
    ds = build_dataset(random_walk)
    idx = ds.labeled
    model = model_cls().fit(ds.X.loc[idx], ds.fwd_return.loc[idx])
    out = model.predict(ds.X.iloc[-20:])
    assert list(out.columns) == ["prob_up", "exp_return"]
    assert len(out) == 20
    assert out["prob_up"].between(0, 1).all()
    assert np.isfinite(out["exp_return"]).all()


def test_ensemble_weights_combine_members(random_walk):
    ds = build_dataset(random_walk)
    idx = ds.labeled
    ens = EnsembleModel(default_models(), weights={"events": 1.0}).fit(ds.X.loc[idx], ds.fwd_return.loc[idx])
    outputs = ens.predict_all(ds.X.iloc[-5:])
    assert np.allclose(outputs["ensemble"]["prob_up"], outputs["events"]["prob_up"])


def test_models_find_planted_momentum(momentum):
    ds = build_dataset(momentum)
    preds = walk_forward(ds, models_factory=lambda: [MarkovModel(), LogisticModel()], min_train=100, step=20)
    metrics = score_predictions(preds)
    # Positive autocorrelation of 0.4 is learnable: both models must beat a coin flip.
    assert metrics.loc["markov", "accuracy"] > 0.53
    assert metrics.loc["logistic", "auc"] > 0.53


def test_analogs_returns_past_neighbours(random_walk):
    ds = build_dataset(random_walk)
    idx = ds.labeled
    model = AnalogModel(k=5).fit(ds.X.loc[idx], ds.fwd_return.loc[idx])
    found = model.analogs(ds.X.iloc[[-1]])
    assert len(found) == 5
    assert (found.index < ds.X.index[-1]).all()
