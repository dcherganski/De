"""24-hour live PAPER soak test: sample the running platform and write a report.

Run it next to a running engine (`python -m app.cli soak --hours 24`). It only reads the database.
The report states what was measured; a run that was cut short says so instead of claiming 24 hours.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy import func, select

from ..core.models import EngineStatus, JevCall, LatencyEvent, Order, Signal
from ..core.timeutil import age_s, utcnow


@dataclass
class Sample:
    ts: str
    engine_alive: bool
    heartbeat_age_s: float | None
    feed_connected: bool
    data_ok: bool
    quote_age_s: float | None
    is_fixture: bool
    jev_ok: int
    jev_failed: int
    signals: int
    orders: int
    last_error: str | None


def collect(sf, now: datetime | None = None, stale_s: float = 15) -> Sample:
    now = now or utcnow()
    with sf() as s:
        st = s.get(EngineStatus, 1)
        jev = dict(s.execute(select(JevCall.status, func.count(JevCall.id)).group_by(JevCall.status)).all())
        signals = s.scalar(select(func.count(Signal.id))) or 0
        orders = s.scalar(select(func.count(Order.id))) or 0
    hb = age_s(st.heartbeat_at, now) if st and st.heartbeat_at else None
    return Sample(ts=now.isoformat(), engine_alive=hb is not None and hb <= stale_s, heartbeat_age_s=hb,
                  feed_connected=bool(st and st.feed_connected), data_ok=bool(st and st.data_ok),
                  quote_age_s=age_s(st.last_quote_at, now) if st and st.last_quote_at else None,
                  is_fixture=bool(st and st.is_fixture), jev_ok=jev.get("ok", 0),
                  jev_failed=sum(v for k, v in jev.items() if k != "ok"), signals=signals, orders=orders,
                  last_error=st.last_error if st else None)


def summarize(samples: list[Sample], planned_hours: float, sf=None) -> str:
    if not samples:
        return "# 24-часов тест\n\nНяма събрани проби.\n"
    n = len(samples)
    first, last = samples[0], samples[-1]
    span_h = (datetime.fromisoformat(last.ts) - datetime.fromisoformat(first.ts)).total_seconds() / 3600
    pct = lambda pred: 100 * sum(1 for x in samples if pred(x)) / n  # noqa: E731
    errors = sorted({x.last_error for x in samples if x.last_error})
    lines = [
        "# 24-часов PAPER тест — отчет",
        "",
        f"- Планирано: {planned_hours:g} ч; реално измерено: **{span_h:.2f} ч** в {n} проби "
        f"({first.ts} → {last.ts}).",
        "- " + ("**Тестът НЕ е пълен** — измерено е по-малко от планираното." if span_h < planned_hours * 0.98
                else "Тестът покрива планирания период."),
        f"- Тестови данни (fixture) в някоя проба: {'да' if any(x.is_fixture for x in samples) else 'не'}.",
        "",
        "| Показател | Стойност |",
        "|---|---|",
        f"| Двигателят отговаря | {pct(lambda x: x.engine_alive):.1f}% от пробите |",
        f"| Потокът е свързан | {pct(lambda x: x.feed_connected):.1f}% |",
        f"| Данните стигат за сделка | {pct(lambda x: x.data_ok):.1f}% |",
        f"| Нови сигнали | {last.signals - first.signals} |",
        f"| Jev: успешни / неуспешни заявки | {last.jev_ok - first.jev_ok} / {last.jev_failed - first.jev_failed} |",
        f"| Нови поръчки (PAPER) | {last.orders - first.orders} |",
    ]
    if sf is not None:
        with sf() as s:
            rows = s.execute(select(LatencyEvent.stage, func.percentile_cont(0.5).within_group(LatencyEvent.ms),
                                    func.percentile_cont(0.95).within_group(LatencyEvent.ms))
                             .where(LatencyEvent.ts >= datetime.fromisoformat(first.ts))
                             .group_by(LatencyEvent.stage)).all()
        if rows:
            lines += ["", "| Етап | p50 ms | p95 ms |", "|---|---|---|"]
            lines += [f"| {st} | {p50:.0f} | {p95:.0f} |" for st, p50, p95 in rows]
    lines += ["", "Грешки, видени по време на теста:" if errors else "Не са записани грешки на двигателя."]
    lines += [f"- `{e}`" for e in errors]
    lines += ["", "Измерено в тази инсталация. Не е гаранция за бъдеща работа или печалба."]
    return "\n".join(lines) + "\n"


def run(sf, hours: float, every_s: float, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = utcnow().strftime("%Y%m%dT%H%M%SZ")
    raw = out_dir / f"soak-{stamp}.jsonl"
    report = out_dir / f"soak-{stamp}.md"
    samples: list[Sample] = []
    deadline = time.monotonic() + hours * 3600
    try:
        while True:
            sample = collect(sf)
            samples.append(sample)
            with raw.open("a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(sample), ensure_ascii=False) + "\n")
            report.write_text(summarize(samples, hours, sf), encoding="utf-8")  # partial report survives a crash
            if time.monotonic() >= deadline:
                break
            time.sleep(every_s)
    except KeyboardInterrupt:
        pass
    report.write_text(summarize(samples, hours, sf), encoding="utf-8")
    return report
