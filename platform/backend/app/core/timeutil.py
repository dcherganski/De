"""UTC everywhere. Naive datetimes are rejected rather than guessed."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC = timezone.utc


def utcnow() -> datetime:
    return datetime.now(UTC)


def ensure_utc(ts: datetime) -> datetime:
    if ts.tzinfo is None:
        raise ValueError("naive datetime: every timestamp must carry a timezone")
    return ts.astimezone(UTC)


def parse_iso(value: str) -> datetime:
    return ensure_utc(datetime.fromisoformat(value.replace("Z", "+00:00")))


def floor_time(ts: datetime, seconds: int) -> datetime:
    epoch = int(ensure_utc(ts).timestamp())
    return datetime.fromtimestamp(epoch - epoch % seconds, UTC)


def age_s(ts: datetime | None, now: datetime | None = None) -> float | None:
    if ts is None:
        return None
    return ((now or utcnow()) - ensure_utc(ts)).total_seconds()


def start_of_day(ts: datetime) -> datetime:
    ts = ensure_utc(ts)
    return ts.replace(hour=0, minute=0, second=0, microsecond=0)


__all__ = ["UTC", "utcnow", "ensure_utc", "parse_iso", "floor_time", "age_s", "start_of_day", "timedelta"]
