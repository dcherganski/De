"""Latest market view held by the engine, and the freshness checks that gate new trades."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from ..core.timeutil import age_s, utcnow
from .types import CandleBar, Quote, TradeTick


@dataclass
class DataCheck:
    ok: bool
    reasons: list[str] = field(default_factory=list)


@dataclass
class MarketState:
    venue: str
    symbol: str
    is_fixture: bool = False
    quote: Quote | None = None
    last_trade: TradeTick | None = None
    last_closed_candle: CandleBar | None = None
    feed_connected: bool = False
    feed_detail: str = ""

    def update(self, event) -> None:
        if isinstance(event, Quote):
            if event.is_sane():
                self.quote = event
        elif isinstance(event, TradeTick):
            self.last_trade = event

    def check(self, max_quote_age_s: float, max_candle_gap_s: float, now: datetime | None = None,
              require_candle: bool = True) -> DataCheck:
        """Every reason here blocks new orders; none of them triggers a sale."""
        now = now or utcnow()
        reasons: list[str] = []
        if not self.feed_connected and not self.is_fixture:
            reasons.append(f"няма връзка с потока на {self.venue} ({self.feed_detail or 'неизвестна причина'})")
        if self.quote is None:
            reasons.append("няма котировка")
        else:
            q_age = age_s(self.quote.ts_received, now)
            if q_age is not None and q_age > max_quote_age_s:
                reasons.append(f"котировката е остаряла: {q_age:.0f} s > {max_quote_age_s:.0f} s")
            if not self.quote.is_sane():
                reasons.append("невалидна котировка (bid/ask)")
        if require_candle:
            if self.last_closed_candle is None:
                reasons.append("няма затворена 5-минутна свещ")
            else:
                c = self.last_closed_candle
                gap = (now - c.open_time).total_seconds() - c.interval_s
                if gap > max_candle_gap_s:
                    reasons.append(f"последната затворена свещ е отпреди {gap:.0f} s")
        return DataCheck(ok=not reasons, reasons=reasons)
