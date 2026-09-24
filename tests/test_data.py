from datetime import datetime, timezone

import pandas as pd
import pytest

from crypto_bot.data import CoinbaseClient, drop_incomplete, load_csv, save_csv, update_cache


class FakeResponse:
    def __init__(self, payload, status=200):
        self.payload = payload
        self.status_code = status

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeSession:
    """Serves daily candles for any requested window, newest first like Coinbase."""

    def __init__(self):
        self.headers = {}
        self.calls = []

    def get(self, url, params, timeout):
        self.calls.append(params)
        start = int(pd.Timestamp(params["start"]).timestamp())
        end = int(pd.Timestamp(params["end"]).timestamp())
        step = params["granularity"]
        first = -(-start // step) * step
        rows = [[t, 9.0, 11.0, 10.0, 10.5, 1.0] for t in range(first, end + 1, step)]
        return FakeResponse(rows[::-1])


def test_client_paginates_and_sorts():
    session = FakeSession()
    client = CoinbaseClient(session=session, pause=0)
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    end = datetime(2025, 1, 1, tzinfo=timezone.utc)
    df = client.candles("BTC-USD", "1d", start=start, end=end)
    assert len(session.calls) == 2  # 366 days > 300 candles per request
    assert df.index.is_monotonic_increasing and df.index.is_unique
    assert len(df) == 367
    assert list(df.columns) == ["open", "high", "low", "close", "volume"]
    assert df["low"].iloc[0] == 9.0 and df["open"].iloc[0] == 10.0


def test_csv_round_trip_keeps_timestamps(tmp_path, random_walk):
    path = tmp_path / "c.csv"
    save_csv(random_walk, path)
    raw = pd.read_csv(path)
    assert raw["time"].iloc[0] == int(random_walk.index[0].timestamp())
    back = load_csv(path)
    pd.testing.assert_frame_equal(back, random_walk, check_freq=False, check_index_type=False, check_exact=False)


def test_load_csv_accepts_iso_dates(tmp_path):
    path = tmp_path / "iso.csv"
    path.write_text("time,open,high,low,close,volume\n2024-01-02,1,2,0.5,1.5,10\n2024-01-01,1,2,0.5,1,10\n")
    df = load_csv(path)
    assert df.index[0] == pd.Timestamp("2024-01-01", tz="UTC")


def test_load_csv_rejects_missing_columns(tmp_path):
    path = tmp_path / "bad.csv"
    path.write_text("time,close\n1,2\n")
    with pytest.raises(ValueError):
        load_csv(path)


def test_drop_incomplete(random_walk):
    last_open = random_walk.index[-1]
    assert len(drop_incomplete(random_walk, "1d", now=last_open + pd.Timedelta(hours=5))) == len(random_walk) - 1
    assert len(drop_incomplete(random_walk, "1d", now=last_open + pd.Timedelta(days=1))) == len(random_walk)


def test_update_cache_merges(tmp_path):
    client = CoinbaseClient(session=FakeSession(), pause=0)
    first = update_cache("BTC-USD", "1d", tmp_path, days=10, client=client)
    again = update_cache("BTC-USD", "1d", tmp_path, days=10, client=client)
    assert again.index.is_unique
    assert len(again) >= len(first)
    assert (tmp_path / "BTC-USD_1d.csv").exists()
