"""Market data: Coinbase public candles (no API key needed) and a local CSV cache."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd
import requests

GRANULARITIES = {"1m": 60, "5m": 300, "15m": 900, "1h": 3600, "6h": 21600, "1d": 86400}
COLUMNS = ["open", "high", "low", "close", "volume"]
EXCHANGE_URL = "https://api.exchange.coinbase.com/products/{product}/candles"
MAX_CANDLES_PER_REQUEST = 300


def granularity_seconds(granularity: str | int) -> int:
    if isinstance(granularity, int):
        return granularity
    if granularity not in GRANULARITIES:
        raise ValueError(f"unsupported granularity {granularity!r}; choose one of {list(GRANULARITIES)}")
    return GRANULARITIES[granularity]


def _to_frame(rows: list[list[float]]) -> pd.DataFrame:
    """Coinbase returns [time, low, high, open, close, volume], newest first."""
    if not rows:
        return pd.DataFrame(columns=COLUMNS, index=pd.DatetimeIndex([], tz="UTC", name="time"))
    raw = pd.DataFrame(rows, columns=["time", "low", "high", "open", "close", "volume"])
    raw["time"] = pd.to_datetime(raw["time"].astype("int64"), unit="s", utc=True)
    return raw.set_index("time")[COLUMNS].astype(float)


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.index.name = "time"
    return df


class CoinbaseClient:
    """Minimal client for Coinbase Exchange public market data."""

    def __init__(self, session: requests.Session | None = None, pause: float = 0.35, timeout: float = 15.0):
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "crypto-bot/0.1")
        self.pause = pause
        self.timeout = timeout

    def candles(
        self,
        product: str,
        granularity: str | int = "1d",
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> pd.DataFrame:
        """All candles in [start, end], paginating 300 candles per request."""
        step = granularity_seconds(granularity)
        end = end or datetime.now(timezone.utc)
        start = start or end - timedelta(seconds=step * MAX_CANDLES_PER_REQUEST)
        chunks = []
        cursor = start
        while cursor < end:
            chunk_end = min(cursor + timedelta(seconds=step * MAX_CANDLES_PER_REQUEST), end)
            resp = self.session.get(
                EXCHANGE_URL.format(product=product),
                params={"granularity": step, "start": cursor.isoformat(), "end": chunk_end.isoformat()},
                timeout=self.timeout,
            )
            if resp.status_code == 429:  # rate limited: back off and retry the same window
                time.sleep(max(self.pause, 1.0) * 2)
                continue
            resp.raise_for_status()
            chunks.append(_to_frame(resp.json()))
            cursor = chunk_end
            if cursor < end:
                time.sleep(self.pause)
        frames = [c for c in chunks if not c.empty]
        if not frames:
            return _to_frame([])
        return _clean(pd.concat(frames))


def load_csv(path: str | Path) -> pd.DataFrame:
    """Read a candle CSV (time as unix seconds or ISO date, plus OHLCV columns)."""
    raw = pd.read_csv(path)
    missing = {"time", *COLUMNS} - set(raw.columns)
    if missing:
        raise ValueError(f"{path}: missing columns {sorted(missing)}")
    t = raw["time"]
    raw["time"] = (
        pd.to_datetime(t.astype("int64"), unit="s", utc=True)
        if pd.api.types.is_numeric_dtype(t)
        else pd.to_datetime(t, utc=True, format="mixed")
    )
    return _clean(raw.set_index("time")[COLUMNS].astype(float))


def save_csv(df: pd.DataFrame, path: str | Path) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = df[COLUMNS].copy()
    epoch = pd.Timestamp("1970-01-01", tz="UTC")
    out.insert(0, "time", ((out.index - epoch) // pd.Timedelta(seconds=1)).astype("int64"))
    out.to_csv(path, index=False)


def drop_incomplete(df: pd.DataFrame, granularity: str | int, now: datetime | None = None) -> pd.DataFrame:
    """Remove the still-forming last candle, whose close is not final yet."""
    if df.empty:
        return df
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    if now.tzinfo is None:
        now = now.tz_localize("UTC")
    closes_at = df.index + pd.Timedelta(seconds=granularity_seconds(granularity))
    return df[closes_at <= now]


def update_cache(
    product: str,
    granularity: str = "1d",
    cache_dir: str | Path = "data",
    days: int = 365,
    client: CoinbaseClient | None = None,
) -> pd.DataFrame:
    """Load cached candles and fetch only what is missing since the last cached bar."""
    client = client or CoinbaseClient()
    path = Path(cache_dir) / f"{product}_{granularity}.csv"
    now = datetime.now(timezone.utc)
    cached = load_csv(path) if path.exists() else None
    if cached is not None and not cached.empty:
        start = cached.index[-1].to_pydatetime()  # refetch the last bar: it may have been incomplete
    else:
        start = now - timedelta(days=days)
    fresh = client.candles(product, granularity, start=start, end=now)
    frames = [f for f in (cached, fresh) if f is not None and not f.empty]
    if not frames:
        raise RuntimeError(f"no candles returned for {product} {granularity}")
    merged = _clean(pd.concat(frames))
    save_csv(merged, path)
    return merged
