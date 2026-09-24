"""Command line interface: python -m crypto_bot <command> [options]."""

from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from .backtest import run_backtest
from .bot import BotConfig, PredictionBot
from .data import GRANULARITIES, update_cache
from .events import event_label, event_study, recent_events
from .journal import append_prediction, evaluate_journal, summarize_journal
from .models import MODEL_LABELS
from .report import format_event_table, format_summary, format_text, render_html, to_json


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--product",
        help="един или няколко Coinbase продукта, разделени със запетая (по подразбиране BTC-USD,ETH-USD,SOL-USD; "
        "с --csv се разпознава от името на файла)",
    )
    p.add_argument("--granularity", default="1d", choices=list(GRANULARITIES), help="размер на свещта")
    p.add_argument("--horizon", type=int, default=1, help="колко свещи напред да се прогнозира")
    p.add_argument("--csv", help="чети свещите от CSV вместо от Coinbase API")
    p.add_argument("--cache-dir", default="data", help="папка за кеширани свещи")
    p.add_argument("--offline", action="store_true", help="използвай само кешираните CSV файлове, без Coinbase API")
    p.add_argument("--days", type=int, default=730, help="дни история при първо изтегляне")
    p.add_argument("--events-file", help="CSV с външни събития (колони date,name), напр. FOMC, халвинг")
    p.add_argument("--min-train", type=int, default=150, help="минимум свещи за обучение в бектеста")
    p.add_argument("--step", type=int, default=10, help="през колко свещи се преобучава в бектеста")
    p.add_argument("--threshold", type=float, default=0.02, help="отстъп от 50%% за BUY/SELL сигнал")
    p.add_argument("--fee", type=float, default=0.001, help="такса на сделка (0.001 = 0.1%%)")
    p.add_argument("--allow-short", action="store_true", help="стратегията може да шортва")


DEFAULT_PRODUCTS = ("BTC-USD", "ETH-USD", "SOL-USD")


def _products(args) -> list[str]:
    raw = args.product
    if raw is None and args.csv:
        # data/ETH-USD_1d.csv -> ETH-USD, the naming used by the cache
        match = re.match(r"([A-Za-z0-9]+-[A-Za-z0-9]+)_", Path(args.csv).name)
        if not match:
            raise SystemExit("--csv: не мога да разпозная продукта от името на файла; посочи --product, напр. BTC-USD")
        raw = match.group(1)
    products = [p.strip().upper() for p in (raw or ",".join(DEFAULT_PRODUCTS)).split(",") if p.strip()]
    if not products:
        raise SystemExit("--product: посочи поне един продукт, напр. BTC-USD")
    if args.csv and len(products) > 1:
        raise SystemExit("--csv работи с един продукт; за няколко използвай --offline с файлове в --cache-dir")
    return products


def _config(args, product: str, backtest: bool = True) -> BotConfig:
    return BotConfig(
        product=product,
        granularity=args.granularity,
        horizon=args.horizon,
        csv=args.csv,
        cache_dir=args.cache_dir,
        days=args.days,
        events_file=args.events_file,
        min_train=args.min_train,
        step=args.step,
        threshold=args.threshold,
        fee=args.fee,
        long_only=not args.allow_short,
        backtest=backtest,
        offline=args.offline,
    )


def _write(path: str, text: str) -> None:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding="utf-8")
    print(f"Записано: {out}", file=sys.stderr)


def cmd_fetch(args) -> int:
    for product in _products(args):
        df = update_cache(product, args.granularity, args.cache_dir, args.days)
        print(f"{product} {args.granularity}: {len(df)} свещи ({df.index[0]} – {df.index[-1]}) → {args.cache_dir}")
    return 0


def cmd_events(args) -> int:
    for product in _products(args):
        bot = PredictionBot(_config(args, product, backtest=False))
        ds = bot.build(bot.load_candles())
        print(f"═══ {product} ═══")
        print(format_event_table(event_study(ds.frame["close"], ds.events, args.horizon)))
        print()
        print("Събития през последните 10 свещи:")
        for ts, name in recent_events(ds.events, bars=10):
            print(f"  {ts}  {event_label(name)}")
        print()
    return 0


def cmd_backtest(args) -> int:
    for product in _products(args):
        bot = PredictionBot(_config(args, product))
        ds = bot.build(bot.load_candles())
        res = run_backtest(ds, args.min_train, args.step, args.threshold, args.fee, not args.allow_short)
        print(f"═══ {product} ═══")
        print(f"Walk-forward бектест: {len(res.predictions)} прогнози "
              f"({res.predictions.index[0]} – {res.predictions.index[-1]}), хоризонт {args.horizon}")
        table = res.metrics.copy()
        table.index = [MODEL_LABELS.get(i, i) for i in table.index]
        with pd.option_context("display.width", 200, "display.max_columns", 20, "display.float_format", "{:.4f}".format):
            print(table)
        print(f"Базово ниво „винаги нагоре“: {res.metrics.attrs['always_up_accuracy']:.4f}")
        print("Стратегия:", {k: round(v, 4) if isinstance(v, float) else v for k, v in res.strategy.items()})
        print("Предложени тегла за ансамбъла:", res.weights)
        print()
    return 0


def _predict_once(args) -> int:
    results = [
        PredictionBot(_config(args, product, backtest=not args.no_backtest)).run() for product in _products(args)
    ]
    if args.json:
        print(to_json(results if len(results) > 1 else results[0]))
    else:
        if len(results) > 1:
            print(format_summary(results))
            print()
        print("\n\n".join(format_text(r) for r in results))
    if args.html:
        _write(args.html, render_html(results))
    if args.log:
        for result in results:
            append_prediction(args.log, result.prediction)
        print(f"Прогнозите са добавени в журнала: {args.log}", file=sys.stderr)
    return 0


def cmd_predict(args) -> int:
    return _predict_once(args)


def cmd_evaluate(args) -> int:
    if not Path(args.log).exists():
        print(f"Журналът {args.log} не съществува. Пусни първо: predict --log {args.log}", file=sys.stderr)
        return 1
    for product in _products(args):
        bot = PredictionBot(_config(args, product, backtest=False))
        evaluated = evaluate_journal(args.log, bot.load_candles(), args.granularity, product)
        summary = summarize_journal(evaluated)
        print(f"{product}: прогнози в журнала {summary['logged']}, с известен резултат {summary['matured']}")
        if summary["matured"]:
            print(f"  Точност на посоката: {summary['direction_accuracy']:.1%}")
            print(f"  В коридор 68%: {summary['coverage_68']:.1%} · в коридор 95%: {summary['coverage_95']:.1%}")
            cols = ["as_of", "horizon", "prob_up", "last_close", "actual_close", "direction_hit", "inside_68"]
            print(evaluated[evaluated["actual_close"].notna()][cols].tail(15).to_string(index=False))
    return 0


def cmd_watch(args) -> int:
    """Re-run the prediction every `interval` seconds, logging each forecast."""
    args.log = args.log or "predictions_log.csv"
    runs = 0
    while True:
        started = datetime.now(timezone.utc)
        print(f"\n[{started:%Y-%m-%d %H:%M:%S} UTC] нова прогноза…")
        try:
            _predict_once(args)
            cmd_evaluate(args)
        except Exception as exc:  # keep the loop alive on network hiccups
            print(f"Грешка: {exc}", file=sys.stderr)
        runs += 1
        if args.iterations and runs >= args.iterations:
            return 0
        time.sleep(args.interval)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crypto-bot",
        description="Крипто бот: учи се от минали пазарни събития и прогнозира следващата свещ.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("fetch", help="изтегли/обнови историческите свещи от Coinbase")
    _common(p)
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("events", help="какво е следвало след всяко пазарно събитие")
    _common(p)
    p.set_defaults(func=cmd_events)

    p = sub.add_parser("backtest", help="walk-forward проверка на моделите")
    _common(p)
    p.set_defaults(func=cmd_backtest)

    for name, func, helptext in (
        ("predict", cmd_predict, "прогноза за следващата свещ"),
        ("watch", cmd_watch, "прогнозирай периодично и води журнал"),
    ):
        p = sub.add_parser(name, help=helptext)
        _common(p)
        p.add_argument("--json", action="store_true", help="изход в JSON")
        p.add_argument("--html", help="запиши HTML табло в този файл")
        p.add_argument("--log", help="добави прогнозата в CSV журнал")
        p.add_argument("--no-backtest", action="store_true", help="пропусни бектеста (по-бързо, без проверка за предимство)")
        if name == "watch":
            p.add_argument("--interval", type=int, default=3600, help="секунди между прогнозите")
            p.add_argument("--iterations", type=int, default=0, help="брой цикли (0 = безкрайно)")
        p.set_defaults(func=func)

    p = sub.add_parser("evaluate", help="оцени минали прогнози от журнала спрямо реалността")
    _common(p)
    p.add_argument("--log", default="predictions_log.csv", help="CSV журнал с прогнози")
    p.set_defaults(func=cmd_evaluate)
    return parser


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):  # Cyrillic output on Windows consoles
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
