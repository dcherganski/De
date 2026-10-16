"""Robinhood: deliberately unavailable.

Checked 2026-10-09 against the official documentation, https://docs.robinhood.com/crypto/trading/:
"The Robinhood Crypto Trading API is available to customers in the United States only."
The Robinhood Europe help centre (https://robinhood.com/eu/en/support/robinhood-crypto/) lists no
API for EU accounts. A European account therefore has no documented programmatic access, and this
adapter refuses every call instead of guessing endpoints or automating a password login.
"""

from __future__ import annotations

from .base import AdapterStatus, BrokerUnavailable

REASON = (
    "Недостъпно: официалният Robinhood Crypto Trading API е само за клиенти в САЩ "
    "(docs.robinhood.com/crypto/trading, проверено на 09.10.2026). За европейски акаунт няма "
    "документиран API, а недокументирани адреси и автоматичен вход с парола не се използват."
)


class RobinhoodAdapter:
    name = "robinhood"

    @staticmethod
    def static_status() -> AdapterStatus:
        return AdapterStatus(
            name="robinhood", available=False, market_data=False, trading=False, live_trading_verified=False,
            reason=REASON,
            docs=["https://docs.robinhood.com/crypto/trading/", "https://robinhood.com/eu/en/support/robinhood-crypto/"],
            checked_on="2026-10-09",
        )

    def status(self) -> AdapterStatus:
        return self.static_status()

    def _refuse(self, *a, **kw):
        raise BrokerUnavailable(REASON)

    get_balances = get_quotes = get_market_rules = place_order = cancel_order = _refuse
    get_order_status = get_fills = _refuse
