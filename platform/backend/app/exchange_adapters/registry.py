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
    from .bybit import BybitAdapter
    from .robinhood import RobinhoodAdapter

    return [
        AdapterStatus(
            name="paper", available=True, market_data=False, trading=True, live_trading_verified=False,
            reason="Симулирани сделки по живи котировки от избрания източник на данни. Не изпраща поръчки никъде.",
        ),
        BybitAdapter.static_status(),
        AdapterStatus(
            name="kraken", available=True, market_data=True, trading=False, live_trading_verified=False,
            reason="Резервен източник на публични цени (WebSocket v2). Търговия през Kraken няма в тази версия.",
            docs=["https://docs.kraken.com/exchange/api-reference/spot-websocket-v2/ticker"], checked_on="2026-10-09",
        ),
        RobinhoodAdapter.static_status(),
    ]


def selectable_venues(s: Settings) -> list[str]:
    """Venues whose public market data this build can stream (the dashboard's exchange choice)."""
    return [v.name for v in venue_statuses(s) if v.available and v.market_data]
