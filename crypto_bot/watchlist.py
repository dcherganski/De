"""Which products the bot follows: an editable watchlist file, or every live Coinbase USD pair."""

from __future__ import annotations

from pathlib import Path

DEFAULT_WATCHLIST = "watchlist.txt"
FALLBACK_PRODUCTS = ("BTC-USD", "ETH-USD", "SOL-USD")

# Pegged to a fiat currency or to gold: nothing to forecast.
STABLECOINS = frozenset(
    {
        "USDT", "USDC", "DAI", "PYUSD", "USD1", "USDG", "RLUSD", "TUSD", "USDD", "FDUSD", "USDS",
        "USDP", "PAX", "GUSD", "EURC", "EURT", "GYEN", "PAXG", "XAUT", "U",
    }
)


def load_watchlist(path: str | Path) -> list[str]:
    """Product ids from a text file: one per line, '#' starts a comment, duplicates dropped."""
    products: list[str] = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        item = line.split("#", 1)[0].strip().upper()
        if item and item not in products:
            products.append(item)
    return products


def is_stablecoin(product: str) -> bool:
    return product.split("-", 1)[0].upper() in STABLECOINS


def select_usd_products(listing: list[dict]) -> list[str]:
    """Filter a Coinbase /products listing down to tradable crypto/USD pairs."""
    selected = []
    for p in listing:
        if p.get("quote_currency") != "USD" or p.get("status") != "online":
            continue
        if p.get("trading_disabled") or p.get("fx_stablecoin") or is_stablecoin(p.get("id", "")):
            continue
        selected.append(p["id"])
    return sorted(selected)
