import json

import pandas as pd
import pytest

from crypto_bot import BotConfig, PredictionBot
from crypto_bot.cli import main
from crypto_bot.data import save_csv
from crypto_bot.journal import append_prediction, evaluate_journal, summarize_journal
from crypto_bot.models import EventStudyModel, MarkovModel
from crypto_bot.report import render_html, to_payload

import crypto_bot.bot as bot_module


def fast_models():
    return [EventStudyModel(), MarkovModel()]


def run_bot(monkeypatch, candles, **cfg):
    monkeypatch.setattr(bot_module, "default_models", fast_models)
    monkeypatch.setattr("crypto_bot.backtest.default_models", fast_models)
    config = BotConfig(min_train=120, step=25, **cfg)
    return PredictionBot(config).run(candles=candles)


def test_bot_prediction_is_complete(monkeypatch, random_walk):
    result = run_bot(monkeypatch, random_walk)
    p = result.prediction
    assert 0 <= p.prob_up <= 1
    assert p.range_95[0] < p.range_68[0] < p.range_68[1] < p.range_95[1]
    assert p.signal in {"BUY", "SELL", "HOLD"}
    assert p.as_of == random_walk.index[-1].isoformat()
    assert pd.Timestamp(p.target_close_time) == random_walk.index[-1] + pd.Timedelta(days=2)
    assert set(p.models) == {"events", "markov", "ensemble"}
    assert result.backtest is not None and p.has_edge in (True, False)
    if not p.has_edge:
        assert p.signal == "HOLD"


def test_report_outputs(monkeypatch, random_walk):
    result = run_bot(monkeypatch, random_walk, horizon=2)
    payload = to_payload(result)
    json.dumps(payload)  # must be serialisable (no NaN/Timestamp objects)
    html = render_html(result)
    assert "/*__BOT_DATA__*/" not in html
    assert html.startswith("<!doctype html>")
    assert '"product": "BTC-USD"' in html or '"product":"BTC-USD"' in html
    assert "<title>" in render_html(result, standalone=False)


def test_journal_round_trip(monkeypatch, random_walk, tmp_path):
    log = tmp_path / "log.csv"
    history, future = random_walk.iloc[:-5], random_walk
    result = run_bot(monkeypatch, history, backtest=False)
    append_prediction(log, result.prediction)
    append_prediction(log, result.prediction)  # same bar again -> replaced, not duplicated
    assert len(pd.read_csv(log)) == 1
    evaluated = evaluate_journal(log, future, "1d", "BTC-USD")
    summary = summarize_journal(evaluated)
    assert summary["matured"] == 1
    target = pd.Timestamp(result.prediction.target_close_time) - pd.Timedelta(days=1)
    assert evaluated["actual_close"].iloc[0] == future.loc[target, "close"]


def test_cli_predict_json(tmp_path, random_walk, capsys):
    path = tmp_path / "BTC-USD_1d.csv"  # product is recognised from the cache-style file name
    save_csv(random_walk, path)
    code = main(["predict", "--csv", str(path), "--no-backtest", "--json", "--min-train", "120",
                 "--html", str(tmp_path / "d.html")])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["prediction"]["product"] == "BTC-USD"
    assert (tmp_path / "d.html").exists()


def test_cli_csv_needs_product_when_name_is_unknown(tmp_path, random_walk):
    path = tmp_path / "prices.csv"
    save_csv(random_walk, path)
    with pytest.raises(SystemExit):
        main(["predict", "--csv", str(path), "--no-backtest"])


def test_cli_predicts_several_products_offline(tmp_path, random_walk, momentum, capsys):
    save_csv(random_walk, tmp_path / "BTC-USD_1d.csv")
    save_csv(momentum, tmp_path / "ETH-USD_1d.csv")
    log = tmp_path / "log.csv"
    code = main(["predict", "--product", "BTC-USD,ETH-USD", "--offline", "--cache-dir", str(tmp_path),
                 "--no-backtest", "--json", "--min-train", "120", "--log", str(log),
                 "--html", str(tmp_path / "multi.html")])
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert [a["prediction"]["product"] for a in out["assets"]] == ["BTC-USD", "ETH-USD"]
    assert sorted(pd.read_csv(log)["product"]) == ["BTC-USD", "ETH-USD"]
    html = (tmp_path / "multi.html").read_text(encoding="utf-8")
    assert html.count('"product": "') + html.count('"product":"') >= 2


def test_offline_mode_reports_missing_cache(tmp_path):
    bot = PredictionBot(BotConfig(product="SOL-USD", offline=True, cache_dir=str(tmp_path)))
    with pytest.raises(FileNotFoundError):
        bot.load_candles()
