import re
from pathlib import Path

import pytest

from crypto_bot.bot import BotConfig, run_many
from crypto_bot.cli import main
from crypto_bot.data import save_csv
from crypto_bot.report import _price, format_summary
from crypto_bot.watchlist import is_stablecoin, load_watchlist, select_usd_products

REPO_WATCHLIST = Path(__file__).resolve().parent.parent / "watchlist.txt"


def test_load_watchlist_skips_comments_and_duplicates(tmp_path):
    path = tmp_path / "w.txt"
    path.write_text("# header\nbtc-usd  # comment\n\nETH-USD\nBTC-USD\n", encoding="utf-8")
    assert load_watchlist(path) == ["BTC-USD", "ETH-USD"]


def test_repo_watchlist_is_valid():
    products = load_watchlist(REPO_WATCHLIST)
    assert len(products) >= 50
    assert products[:3] == ["BTC-USD", "ETH-USD", "ZEC-USD"]
    assert all(re.fullmatch(r"[A-Z0-9]+-USD", p) for p in products)
    assert not any(is_stablecoin(p) for p in products)


def test_select_usd_products_filters_listing():
    listing = [
        {"id": "BTC-USD", "quote_currency": "USD", "status": "online"},
        {"id": "ETH-USDC", "quote_currency": "USDC", "status": "online"},
        {"id": "USDT-USD", "quote_currency": "USD", "status": "online"},
        {"id": "OLD-USD", "quote_currency": "USD", "status": "delisted"},
        {"id": "HALT-USD", "quote_currency": "USD", "status": "online", "trading_disabled": True},
        {"id": "ADA-USD", "quote_currency": "USD", "status": "online"},
    ]
    assert select_usd_products(listing) == ["ADA-USD", "BTC-USD"]


def test_price_format_covers_tiny_prices():
    assert _price(84378.31) == "$84,378"
    assert _price(114.99) == "$114.99"
    assert _price(0.00000352) == "$0.000003520"


def test_run_many_skips_missing_and_short_history(tmp_path, random_walk):
    save_csv(random_walk, tmp_path / "AAA-USD_1d.csv")
    save_csv(random_walk.iloc[:100], tmp_path / "NEW-USD_1d.csv")  # listed recently: too little history
    save_csv(random_walk * 2, tmp_path / "BBB-USD_1d.csv")
    configs = [
        BotConfig(product=p, offline=True, cache_dir=str(tmp_path), backtest=False, min_train=120)
        for p in ("AAA-USD", "NEW-USD", "MISSING-USD", "BBB-USD")
    ]
    results, skipped = run_many(configs, jobs=2)
    assert [r.config.product for r in results] == ["AAA-USD", "BBB-USD"]
    reasons = dict(skipped)
    assert set(reasons) == {"NEW-USD", "MISSING-USD"}
    assert "недостатъчна история" in reasons["NEW-USD"]
    summary = format_summary(results, skipped)
    assert "Пропуснати (2)" in summary and "AAA-USD" in summary


def test_cli_all_offline_uses_every_cached_product(tmp_path, random_walk, capsys):
    for product in ("AAA-USD", "BBB-USD", "CCC-USD"):
        save_csv(random_walk, tmp_path / f"{product}_1d.csv")
    code = main(["predict", "--product", "all", "--offline", "--cache-dir", str(tmp_path),
                 "--no-backtest", "--json", "--min-train", "120", "--jobs", "1"])
    assert code == 0
    import json

    out = json.loads(capsys.readouterr().out)
    assert [a["prediction"]["product"] for a in out["assets"]] == ["AAA-USD", "BBB-USD", "CCC-USD"]
    assert out["skipped"] == []


def test_cli_default_reads_watchlist(tmp_path, random_walk, capsys):
    save_csv(random_walk, tmp_path / "AAA-USD_1d.csv")
    watch = tmp_path / "list.txt"
    watch.write_text("AAA-USD\nZZZ-USD  # not cached\n", encoding="utf-8")
    code = main(["predict", "--watchlist", str(watch), "--offline", "--cache-dir", str(tmp_path),
                 "--no-backtest", "--min-train", "120", "--jobs", "1"])
    assert code == 0
    out = capsys.readouterr().out
    assert "AAA-USD" in out and "Пропуснати (1)" in out and "ZZZ-USD" in out


def test_csv_product_still_single(tmp_path, random_walk):
    path = tmp_path / "BTC-USD_1d.csv"
    save_csv(random_walk, path)
    with pytest.raises(SystemExit):
        main(["predict", "--csv", str(path), "--product", "BTC-USD,ETH-USD", "--no-backtest"])
