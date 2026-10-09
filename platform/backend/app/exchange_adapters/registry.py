"""Which venues exist, what each can do in this build, and why. Shown on the dashboard.

Choosing a venue in the dashboard changes which quotes and broker the engine uses after a restart.
It never moves funds between venues: balances on different venues are separate accounts.
"""

from __future__ import annotations

from ..core.config import Settings
from .base import AdapterStatus

SUPPORTED_SYMBOLS = [
    {"symbol": "BTC/EUR", "supported": True, "note": ""},
    {"symbol": "ETH/EUR", "supported": False,
     "note": "Не се поддържа: началният портфейл е в BTC и отчетите са в EUR; друга двойка иска нов портфейл."},
]


def venue_statuses(s: Settings) -> list[AdapterStatus]:
    statuses = [
        AdapterStatus(
            name="paper", available=True, market_data=False, trading=True, live_trading_verified=False,
            reason="Симулирани сделки по живи котировки от избрания източник на данни. Не изпраща поръчки никъде.",
        )
    ]
    try:
        from .kraken import KrakenAdapter
        statuses.append(KrakenAdapter.static_status())
    except ImportError:
        statuses.append(AdapterStatus("kraken", True, True, False, False,
                                      "Публични пазарни данни (WebSocket v2). Търговският адаптер не е в тази версия."))
    try:
        from .coinbase import CoinbaseAdapter
        statuses.append(CoinbaseAdapter.static_status())
    except ImportError:
        statuses.append(AdapterStatus("coinbase", False, False, False, False, "Адаптерът не е в тази версия."))
    from .robinhood import RobinhoodAdapter
    statuses.append(RobinhoodAdapter.static_status())
    return statuses


def selectable_venues(s: Settings) -> list[str]:
    """Venues whose public market data this build can stream (the dashboard's exchange choice)."""
    return [v.name for v in venue_statuses(s) if v.available and v.market_data]
