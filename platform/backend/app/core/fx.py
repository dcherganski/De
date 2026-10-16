"""EUR reference rates for reporting (Jev is billed in USD). Never used for trading decisions.

Source: the ECB euro foreign exchange reference rates, published once per working day
(https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml). When the feed cannot be read the
configured fallback is used and labelled as such.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal

import httpx
from defusedxml import ElementTree

ECB_URL = "https://www.ecb.europa.eu/stats/eurofxref/eurofxref-daily.xml"


@dataclass(frozen=True)
class FxRate:
    usd_per_eur: Decimal
    source: str


def parse_ecb(xml: bytes) -> dict[str, Decimal]:
    root = ElementTree.fromstring(xml)
    rates = {}
    for el in root.iter():
        cur, rate = el.attrib.get("currency"), el.attrib.get("rate")
        if cur and rate:
            rates[cur] = Decimal(rate)
    return rates


class FxProvider:
    def __init__(self, fallback_usd_per_eur: Decimal, ttl_s: int = 6 * 3600,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.fallback = fallback_usd_per_eur
        self.ttl_s = ttl_s
        self.transport = transport
        self._cached: FxRate | None = None
        self._at = 0.0

    async def usd_per_eur(self) -> FxRate:
        if self._cached is not None and time.monotonic() - self._at < self.ttl_s:
            return self._cached
        try:
            async with httpx.AsyncClient(timeout=5, transport=self.transport) as c:
                r = await c.get(ECB_URL)
                r.raise_for_status()
                usd = parse_ecb(r.content)["USD"]
            self._cached = FxRate(usd, "ECB reference rate")
        except Exception:
            self._cached = FxRate(self.fallback, "fallback from config (ECB unavailable)")
        self._at = time.monotonic()
        return self._cached

    async def usd_to_eur(self, usd: Decimal) -> tuple[Decimal, FxRate]:
        rate = await self.usd_per_eur()
        return usd / rate.usd_per_eur, rate
