"""Human-readable (Bulgarian) text report, JSON payload and the HTML dashboard."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from importlib import resources

import numpy as np
import pandas as pd

from .bot import BotResult
from .models import MODEL_LABELS

SIGNAL_LABELS = {"BUY": "КУПИ", "SELL": "ПРОДАЙ", "HOLD": "ИЗЧАКАЙ"}
DISCLAIMER = (
    "Това е статистически инструмент, а не финансов съвет. Криптовалутите са силно "
    "волатилни; миналите закономерности не гарантират бъдещи резултати."
)


def _clean(obj):
    """Make numpy/pandas values JSON-safe (NaN -> None, timestamps -> ISO)."""
    if isinstance(obj, dict):
        return {str(k): _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (pd.Timestamp, datetime)):
        return None if pd.isna(obj) else pd.Timestamp(obj).isoformat()
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (float, np.floating)):
        return None if math.isnan(float(obj)) else float(obj)
    if obj is pd.NaT:
        return None
    return obj


def to_payload(result: BotResult, history_bars: int = 120) -> dict:
    ds = result.dataset
    frame = ds.frame
    hist = frame.iloc[-history_bars:]
    payload = {
        "meta": {
            "product": result.config.product,
            "granularity": result.config.granularity,
            "horizon": result.config.horizon,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "n_candles": len(frame),
            "first_candle": frame.index[0],
            "last_candle": frame.index[-1],
            "disclaimer": DISCLAIMER,
        },
        "prediction": result.prediction.to_dict(),
        "history": [
            {"t": ts, "o": r.open, "h": r.high, "l": r.low, "c": r.close, "v": r.volume}
            for ts, r in hist[["open", "high", "low", "close", "volume"]].iterrows()
        ],
        "volatility": result.volatility,
        "events": {
            "base_rate": result.event_table.attrs.get("base_rate"),
            "base_mean": result.event_table.attrs.get("base_mean"),
            "rows": result.event_table.reset_index().to_dict(orient="records"),
        },
        "backtest": None,
    }
    bt = result.backtest
    if bt is not None:
        metrics = bt.metrics.reset_index()
        metrics["label"] = metrics["model"].map(lambda m: MODEL_LABELS.get(m, m))
        payload["backtest"] = {
            "metrics": metrics.to_dict(orient="records"),
            "always_up_accuracy": bt.metrics.attrs.get("always_up_accuracy"),
            "strategy": bt.strategy,
            "weights": bt.weights,
            "first_test": bt.predictions.index[0],
            "last_test": bt.predictions.index[-1],
            "equity": [
                {"t": ts, "s": r.strategy, "b": r.buy_and_hold}
                for ts, r in bt.equity[["strategy", "buy_and_hold"]].iterrows()
            ],
        }
    return _clean(payload)


def _as_list(results: BotResult | list[BotResult]) -> list[BotResult]:
    return list(results) if isinstance(results, (list, tuple)) else [results]


def to_multi_payload(results: BotResult | list[BotResult]) -> dict:
    return {"assets": [to_payload(r) for r in _as_list(results)]}


def to_json(results: BotResult | list[BotResult], indent: int | None = 2) -> str:
    """One asset -> its payload; several -> {"assets": [...]}."""
    payload = to_payload(results) if isinstance(results, BotResult) else to_multi_payload(results)
    return json.dumps(payload, ensure_ascii=False, indent=indent)


def _pct(x, digits: int = 1, sign: bool = False) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    return f"{x * 100:+.{digits}f}%" if sign else f"{x * 100:.{digits}f}%"


def _money(x: float) -> str:
    return f"${x:,.2f}"


def format_summary(results: list[BotResult]) -> str:
    """One line per asset, so several forecasts can be compared at a glance."""
    lines = [
        f"{'актив':<9} {'цена':>12} {'ръст':>7} {'очаквано':>9} {'коридор 68%':>25}  сигнал",
    ]
    for r in results:
        p = r.prediction
        edge = "" if p.has_edge is None else (" ✓ предимство" if p.has_edge else " (без предимство)")
        corridor = f"{_money(p.range_68[0])} – {_money(p.range_68[1])}"
        lines.append(
            f"{p.product:<9} {_money(p.last_close):>12} {_pct(p.prob_up):>7} "
            f"{_pct(math.expm1(p.exp_return), 2, sign=True):>9} {corridor:>25}  "
            f"{SIGNAL_LABELS.get(p.signal, p.signal)}{edge}"
        )
    return "\n".join(lines)


def format_text(result: BotResult) -> str:
    p = result.prediction
    cfg = result.config
    lines = []
    add = lines.append
    add(f"═══ {p.product} · свещи {p.granularity} · прогноза {p.horizon} период(а) напред ═══")
    add(f"Последна затворена свещ: {p.as_of}   Цена на затваряне: {_money(p.last_close)}")
    add(f"Прогнозата е за затварянето в: {p.target_close_time}")
    add("")
    add(f"  Вероятност за ръст:   {_pct(p.prob_up)}")
    add(f"  Очаквана промяна:     {_pct(math.expm1(p.exp_return), 2, sign=True)}  →  {_money(p.expected_price)}")
    add(f"  Диапазон 68%:         {_money(p.range_68[0])} – {_money(p.range_68[1])}")
    add(f"  Диапазон 95%:         {_money(p.range_95[0])} – {_money(p.range_95[1])}")
    add(f"  Сигнал:               {SIGNAL_LABELS.get(p.signal, p.signal)}  ({p.signal_reason})")
    add("")
    add("Гласове на моделите (вероятност за ръст · тегло):")
    for name, m in p.models.items():
        if name == "ensemble":
            continue
        add(f"  {m['label']:<34} {_pct(m['prob_up']):>7} · {m['weight']:.2f}")
    add("")
    if p.active_events:
        add("Активни събития на последната свещ и какво е следвало исторически:")
        for e in p.active_events:
            add(f"  • {e['label']}: {e['count']} случая, ръст след тях в {_pct(e['hit_rate'])}, "
                f"средно {_pct(e['mean_return'], 2, sign=True)}")
    else:
        add("Няма активни събития на последната свещ.")
    if p.upcoming_events:
        add("Предстоящи външни събития в прогнозния прозорец (очаквай по-висока волатилност):")
        for e in p.upcoming_events:
            add(f"  {e['time'][:16]}  {e['label']}")
    if p.recent_events:
        add("Събития през последните 10 свещи:")
        for e in p.recent_events[:8]:
            add(f"  {e['time'][:16]}  {e['label']}")
    if p.analogs:
        add("Най-сходни минали ситуации (k-NN аналози) и какво е последвало:")
        for a in p.analogs:
            add(f"  {a['time'][:10]}  → {_pct(math.expm1(a['fwd_return']), 2, sign=True)}")
    add("")
    v = result.volatility
    add(f"Калибрация на диапазона (walk-forward, {v['n']} прогнози): "
        f"68% → {_pct(v['coverage_68'])}, 95% → {_pct(v['coverage_95'])}")
    bt = result.backtest
    if bt is not None:
        add("")
        add(f"Walk-forward бектест ({bt.predictions.index[0].date()} – {bt.predictions.index[-1].date()}, "
            f"{len(bt.predictions)} прогнози):")
        add(f"  {'модел':<34} {'точност':>8} {'Brier':>7} {'skill':>7} {'AUC':>6}")
        for name, row in bt.metrics.iterrows():
            add(f"  {MODEL_LABELS.get(name, name):<34} {_pct(row['accuracy']):>8} {row['brier']:>7.4f} "
                f"{row['brier_skill']:>+7.3f} {row['auc']:>6.3f}")
        add(f"  Базово ниво „винаги нагоре“: {_pct(bt.metrics.attrs.get('always_up_accuracy'))}")
        s = bt.strategy
        add(f"  Стратегия по сигнала (такса {_pct(cfg.fee, 2)}): {_pct(s['total_return'], 1, True)} "
            f"срещу купи-и-дръж {_pct(s['buy_and_hold_return'], 1, True)}; "
            f"Sharpe {s['sharpe']:.2f}; макс. просадка {_pct(s['max_drawdown'])}; сделки {s['trades']}")
    add("")
    add(DISCLAIMER)
    return "\n".join(lines)


def format_event_table(t: pd.DataFrame) -> str:
    lines = [
        f"Какво е следвало {t.attrs['horizon']} период(а) след всяко събитие "
        f"(базово ниво на ръст: {_pct(t.attrs['base_rate'])}):",
        f"  {'събитие':<42} {'брой':>5} {'ръст':>7} {'разлика':>8} {'ср. доходност':>13} {'t':>6}",
    ]
    for _, r in t.iterrows():
        lines.append(
            f"  {r['label']:<42} {r['count']:>5} {_pct(r['hit_rate']):>7} {_pct(r['edge_vs_base'], 1, True):>8} "
            f"{_pct(r['mean_return'], 2, True):>13} {r['t_stat']:>6.2f}"
        )
    return "\n".join(lines)


def _template() -> str:
    return resources.files("crypto_bot").joinpath("templates/dashboard.html").read_text(encoding="utf-8")


def render_html(results: BotResult | list[BotResult], standalone: bool = True) -> str:
    """Fill the dashboard template with one or more results. `standalone` adds the
    document skeleton so the file opens correctly straight from disk."""
    data = json.dumps(to_multi_payload(results), ensure_ascii=False).replace("</", "<\\/")
    body = _template().replace("/*__BOT_DATA__*/null", data)
    if not standalone:
        return body
    return (
        '<!doctype html>\n<html lang="bg">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">\n'
        "</head>\n<body>\n" + body + "\n</body>\n</html>\n"
    )
