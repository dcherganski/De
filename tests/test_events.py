import numpy as np
import pandas as pd

from crypto_bot.events import detect_events, event_study, load_external_events
from crypto_bot.indicators import add_indicators

from .conftest import make_ohlcv


def test_golden_and_death_cross_detected():
    # Down trend, then a strong up trend, then down again.
    r = np.r_[np.full(80, -0.01), np.full(80, 0.02), np.full(80, -0.02)]
    events = detect_events(add_indicators(make_ohlcv(r)))
    golden = events.index[events["golden_cross"]]
    death = events.index[events["death_cross"]]
    assert len(golden) >= 1 and len(death) >= 1
    assert golden[0] > events.index[80] and death[-1] > events.index[160]


def test_streak_and_breakout(random_walk):
    ind = add_indicators(random_walk)
    ev = detect_events(ind)
    up = ind["close"] > ind["open"]
    expected = up & up.shift(1, fill_value=False) & up.shift(2, fill_value=False)
    assert ev["streak_up_3"].equals(expected.rename("streak_up_3"))
    assert (ind.loc[ev["breakout_high"], "close"] > ind.loc[ev["breakout_high"], "high_n"]).all()


def test_event_study_hit_rate():
    idx = pd.date_range("2024-01-01", periods=6, freq="D", tz="UTC")
    close = pd.Series([100, 110, 100, 110, 100, 110.0], index=idx)
    events = pd.DataFrame({"flag": [True, False, True, False, True, False]}, index=idx)
    table = event_study(close, events, horizon=1)
    row = table.loc["flag"]
    assert row["count"] == 3
    assert row["hit_rate"] == 1.0  # every flagged bar was followed by a rise
    assert np.isclose(row["mean_return"], np.log(1.1))


def test_external_events_are_mapped_to_bars(tmp_path):
    idx = pd.date_range("2024-01-01", periods=10, freq="D", tz="UTC")
    path = tmp_path / "events.csv"
    path.write_text("date,name\n2024-01-03T15:00:00Z,FOMC\n2024-01-07,Bitcoin Halving\n2023-01-01,too old\n")
    ext = load_external_events(path, idx)
    assert set(ext.columns) == {"ext_fomc", "ext_bitcoin_halving", "ext_too_old"}
    assert ext.index[ext["ext_fomc"]].tolist() == [idx[2]]
    assert ext.index[ext["ext_bitcoin_halving"]].tolist() == [idx[6]]
    assert not ext["ext_too_old"].any()


def test_events_after_last_bar_are_not_flagged(tmp_path):
    idx = pd.date_range("2024-01-01", periods=5, freq="D", tz="UTC")
    path = tmp_path / "events.csv"
    path.write_text("date,name\n2024-01-05T10:00:00Z,inside\n2024-01-09,future\n")
    ext = load_external_events(path, idx)
    assert ext["ext_inside"].iloc[-1]
    assert not ext["ext_future"].any()


def test_upcoming_events_window(tmp_path):
    from crypto_bot.events import upcoming_events

    path = tmp_path / "events.csv"
    path.write_text("date,name\n2024-01-02T18:00:00Z,FOMC decision\n2024-01-05,Later\n")
    found = upcoming_events(path, pd.Timestamp("2024-01-02", tz="UTC"), pd.Timestamp("2024-01-03", tz="UTC"))
    assert [e["event"] for e in found] == ["ext_fomc_decision"]
