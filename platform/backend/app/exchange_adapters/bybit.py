"""Bybit V5 account adapter (spot only, no leverage). Trading is locked in this build.

Checked against the official docs (bybit-exchange.github.io/docs/v5) on 2026-10-09:
- Auth (/v5/guide): HMAC-SHA256 over timestamp + api_key + recv_window + (query string | JSON body),
  lowercase hex, sent as X-BAPI-API-KEY / X-BAPI-TIMESTAMP / X-BAPI-SIGN / X-BAPI-RECV-WINDOW.
  EEA accounts use https://api.bybit.eu, global accounts https://api.bybit.com.
- POST /v5/order/create (/v5/order/create-order): category=spot, side Buy/Sell, orderType Market,
  qty; spot market Buy is by value unless marketUnit=baseCoin, so we always send baseCoin; orderLinkId
  up to 36 chars and unique (our client_order_id, a UUID, is 36); isLeverage=0. The ack is asynchronous.
- GET /v5/order/realtime (open + recent 500 closed, by orderLinkId), GET /v5/order/history for older.
- POST /v5/order/cancel with orderId or orderLinkId.
- GET /v5/execution/list by orderLinkId: execId, execPrice, execQty, execFee, feeCurrency, execTime.
  Spot taker fees are charged in the coin received (Buy -> base coin, Sell -> quote coin) (/v5/enum).
- GET /v5/account/wallet-balance?accountType=UNIFIED: coin[].walletBalance, locked, spotBorrow.
- GET /v5/user/query-api: readOnly, permissions.Spot / .Wallet (Withdraw, AccountTransfer), ips, type.
- Error codes (/v5/error): 10002 time window, 10004 signature, 10006 rate limit, 110004 wallet balance
  insufficient, 170131 balance insufficient, 110001 / 170213 order does not exist.

Order placement and cancellation raise BrokerUnavailable unless the adapter is built with
trading_enabled=True, which nothing in this build does. Reading balances, orders and the key's
permissions works with a key so the setup can be verified without risking money.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from collections.abc import Callable
from decimal import Decimal
from urllib.parse import urlencode

import httpx

from ..core.numbers import ZERO
from ..market_data.bybit import (
    REST_URLS,
    BybitError,
    check_ret,
    ms_to_dt,
    parse_instrument,
    parse_orderbook,
    public_get,
    to_bybit_symbol,
)
from ..market_data.types import MarketRules, Quote
from .base import (
    AdapterStatus,
    BalanceInfo,
    BrokerError,
    BrokerTimeout,
    BrokerUnavailable,
    FillInfo,
    OrderRequest,
    OrderState,
    OrderStatus,
)

DOCS = [
    "https://bybit-exchange.github.io/docs/v5/guide",
    "https://bybit-exchange.github.io/docs/v5/order/create-order",
    "https://bybit-exchange.github.io/docs/v5/user/apikey-info",
    "https://announcements.bybit.eu/en-EU/article/api-trading-power-your-tools-and-ai-with-deep-liquidity-and-low-latency--art639fafc104c9/",
]
LOCKED = ("Търговията през Bybit е заключена в тази версия: реални поръчки се включват само с твое изрично "
          "разрешение, ALLOW_LIVE_TRADING=true и проверен край до край адаптер.")
# Codes after which Bybit has certainly not created the order (validation or auth failures).
DEFINITE_REJECTS = {10002, 10004, 10006, 110004, 170131}
NOT_FOUND_CODES = {110001, 170213}
STATUS_MAP = {
    "Created": OrderStatus.OPEN,
    "New": OrderStatus.OPEN,
    "Untriggered": OrderStatus.OPEN,
    "Triggered": OrderStatus.OPEN,
    "PartiallyFilled": OrderStatus.PARTIALLY_FILLED,
    "Filled": OrderStatus.FILLED,
    "Cancelled": OrderStatus.CANCELLED,
    "PartiallyFilledCanceled": OrderStatus.CANCELLED,
    "Deactivated": OrderStatus.CANCELLED,
    "Rejected": OrderStatus.REJECTED,
}


def sign(secret: str, timestamp_ms: str, api_key: str, recv_window: str, payload: str) -> str:
    return hmac.new(secret.encode(), f"{timestamp_ms}{api_key}{recv_window}{payload}".encode(), hashlib.sha256).hexdigest()


class BybitClient:
    """Signed REST calls. The secret never leaves this object and is not part of its repr."""

    def __init__(self, base_url: str, api_key: str, api_secret: str, recv_window: int = 5000, timeout_s: float = 10,
                 transport: httpx.AsyncBaseTransport | None = None, now_ms: Callable[[], int] | None = None):
        self.base_url = base_url
        self._key = api_key
        self._secret = api_secret
        self.recv_window = str(recv_window)
        self.timeout_s = timeout_s
        self.transport = transport
        self.now_ms = now_ms or (lambda: int(time.time() * 1000))

    def __repr__(self) -> str:
        return f"BybitClient(base_url={self.base_url!r})"

    def _headers(self, payload: str) -> dict[str, str]:
        ts = str(self.now_ms())
        return {"X-BAPI-API-KEY": self._key, "X-BAPI-TIMESTAMP": ts, "X-BAPI-RECV-WINDOW": self.recv_window,
                "X-BAPI-SIGN": sign(self._secret, ts, self._key, self.recv_window, payload)}

    async def _send(self, method: str, path: str, query: str = "", body: str | None = None) -> dict:
        headers = self._headers(body if body is not None else query)
        if body is not None:
            headers["Content-Type"] = "application/json"
        url = f"{self.base_url}{path}" + (f"?{query}" if query else "")
        try:
            async with httpx.AsyncClient(timeout=self.timeout_s, transport=self.transport) as client:
                resp = await client.request(method, url, headers=headers, content=body)
        except httpx.TimeoutException as exc:
            raise BrokerTimeout(f"Bybit {path}: timeout") from exc
        except httpx.HTTPError as exc:
            raise BrokerError(f"Bybit {path}: {type(exc).__name__}") from exc
        if resp.status_code == 403:
            raise BrokerUnavailable(f"Bybit {path}: HTTP 403 (IP ограничение или блокиран регион)")
        if resp.status_code >= 500:
            raise BrokerError(f"Bybit {path}: HTTP {resp.status_code}")
        try:
            return resp.json()
        except ValueError as exc:
            raise BrokerError(f"Bybit {path}: HTTP {resp.status_code}, отговорът не е JSON") from exc

    async def get(self, path: str, params: dict) -> dict:
        return await self._send("GET", path, query=urlencode(params))

    async def post(self, path: str, body: dict) -> dict:
        return await self._send("POST", path, body=json.dumps(body, separators=(",", ":")))


def _dec(value) -> Decimal:
    return Decimal(value) if value not in (None, "") else ZERO


class BybitAdapter:
    name = "bybit"

    def __init__(self, symbol: str, region: str = "eu", credentials: dict[str, str] | None = None,
                 trading_enabled: bool = False, transport: httpx.AsyncBaseTransport | None = None,
                 now_ms: Callable[[], int] | None = None):
        self.symbol = symbol
        self.bsym = to_bybit_symbol(symbol)
        self.base_asset, self.quote_asset = symbol.split("/")
        self.region = region
        self.base_url = REST_URLS[region]
        self.trading_enabled = trading_enabled
        self.transport = transport
        self.client = (BybitClient(self.base_url, credentials["api_key"], credentials["api_secret"],
                                   transport=transport, now_ms=now_ms) if credentials else None)

    # ------------------------------------------------------------------ info
    @staticmethod
    def static_status() -> AdapterStatus:
        return AdapterStatus(
            name="bybit", available=True, market_data=True, trading=False, live_trading_verified=False,
            reason=("Основна борса. Публични цени (spot, BTC/EUR) и проверка на ключа. " + LOCKED +
                    " За сметка в ЕИП (Bybit EU) REST адресът е api.bybit.eu; цените идват от публичния поток "
                    "stream.bybit.com и не са цена на изпълнение в Bybit EU."),
            docs=DOCS, checked_on="2026-10-09",
            key_permissions="API Transaction, Read-Write само за Spot (SpotTrade); без Wallet (Withdraw, "
                            "AccountTransfer); по възможност с IP ограничение.",
        )

    def status(self) -> AdapterStatus:
        return self.static_status()

    def _auth(self) -> BybitClient:
        if self.client is None:
            raise BrokerUnavailable("Няма въведен Bybit ключ.")
        return self.client

    def _require_trading(self) -> None:
        if not self.trading_enabled:
            raise BrokerUnavailable(LOCKED)

    # ------------------------------------------------------------------ public data
    async def get_market_rules(self, symbol: str) -> MarketRules:
        payload = await public_get(self.base_url, "/v5/market/instruments-info",
                                   {"category": "spot", "symbol": to_bybit_symbol(symbol)}, self.transport)
        return parse_instrument(payload, symbol)

    async def get_quotes(self, symbol: str) -> Quote | None:
        payload = await public_get(self.base_url, "/v5/market/orderbook",
                                   {"category": "spot", "symbol": to_bybit_symbol(symbol), "limit": 1}, self.transport)
        return parse_orderbook(payload, symbol)

    # ------------------------------------------------------------------ account (read)
    async def check_key(self) -> dict:
        """Read the key's permissions. A key that can withdraw or transfer funds is refused."""
        res = check_ret(await self._auth().get("/v5/user/query-api", {}))
        perms = res.get("permissions") or {}
        wallet = list(perms.get("Wallet") or [])
        spot = list(perms.get("Spot") or [])
        problems = []
        if wallet:
            problems.append(f"ключът има права Wallet {wallet} (теглене/прехвърляне) — създай нов ключ без тях")
        other = [k for k, v in perms.items() if v and k not in ("Spot", "Wallet")]
        if other:
            problems.append(f"излишни права: {', '.join(sorted(other))}")
        read_only = int(res.get("readOnly", 1)) == 1
        return {
            "ok": not problems,
            "problems": problems,
            "read_only": read_only,
            "can_trade_spot": not read_only and "SpotTrade" in spot,
            "ip_restricted": bool(res.get("ips")) and res.get("ips") != ["*"],
            "key_type": {1: "личен", 2: "свързан с външно приложение"}.get(res.get("type"), str(res.get("type"))),
            "unified_account": res.get("uta") == 1,
            "expires_at": res.get("expiredAt"),
        }

    async def get_balances(self) -> dict[str, BalanceInfo]:
        res = check_ret(await self._auth().get("/v5/account/wallet-balance", {"accountType": "UNIFIED"}))
        out: dict[str, BalanceInfo] = {}
        for account in res.get("list") or []:
            for c in account.get("coin") or []:
                wallet, locked, borrowed = _dec(c.get("walletBalance")), _dec(c.get("locked")), _dec(c.get("spotBorrow"))
                out[c["coin"]] = BalanceInfo(c["coin"], free=max(ZERO, wallet - locked - borrowed), locked=locked)
        return out

    def _state(self, cid: str, o: dict) -> OrderState:
        fee = ZERO
        avg = _dec(o.get("avgPrice")) or None
        for coin, amount in (o.get("cumFeeDetail") or {}).items():
            amount = _dec(amount)
            fee += amount * avg if coin == self.base_asset and avg else (amount if coin == self.quote_asset else ZERO)
        return OrderState(
            client_order_id=cid, status=STATUS_MAP.get(o.get("orderStatus", ""), OrderStatus.UNKNOWN),
            qty=_dec(o.get("qty")), filled_qty=_dec(o.get("cumExecQty")), avg_price=avg, fee_quote=fee,
            exchange_order_id=o.get("orderId"),
            reject_reason=None if o.get("rejectReason") in (None, "", "EC_NoError") else o.get("rejectReason"),
        )

    async def get_order_status(self, client_order_id: str) -> OrderState:
        client = self._auth()
        for path in ("/v5/order/realtime", "/v5/order/history"):
            payload = await client.get(path, {"category": "spot", "orderLinkId": client_order_id})
            try:
                items = check_ret(payload).get("list") or []
            except BybitError as exc:
                if exc.code in NOT_FOUND_CODES:
                    continue
                raise BrokerError(str(exc)) from exc
            for o in items:
                if o.get("orderLinkId") == client_order_id:
                    return self._state(client_order_id, o)
        return OrderState(client_order_id, OrderStatus.NOT_FOUND)

    async def get_fills(self, client_order_id: str | None = None) -> list[FillInfo]:
        params = {"category": "spot", "symbol": self.bsym, "limit": 100}
        if client_order_id:
            params["orderLinkId"] = client_order_id
        try:
            items = check_ret(await self._auth().get("/v5/execution/list", params)).get("list") or []
        except BybitError as exc:
            raise BrokerError(str(exc)) from exc
        fills = []
        for e in items:
            if e.get("execType") not in (None, "", "Trade"):
                continue
            qty, price, fee = _dec(e.get("execQty")), _dec(e.get("execPrice")), _dec(e.get("execFee"))
            if e.get("feeCurrency") == self.base_asset:
                qty -= fee  # a buy pays its fee in BTC: we receive the net amount
                fee_quote = fee * price
            else:
                fee_quote = fee
            fills.append(FillInfo(venue_fill_id=str(e["execId"]), client_order_id=e.get("orderLinkId") or "",
                                  qty=qty, price=price, fee_quote=fee_quote, ts=ms_to_dt(e["execTime"])))
        return sorted(fills, key=lambda f: f.ts)

    # ------------------------------------------------------------------ trading (locked)
    def order_body(self, req: OrderRequest) -> dict:
        if req.order_type != "market" or req.side not in ("buy", "sell"):
            raise BrokerError("поддържат се само пазарни spot поръчки buy/sell")
        if len(req.client_order_id) > 36:
            raise BrokerError("orderLinkId е по-дълъг от 36 знака")
        return {"category": "spot", "symbol": to_bybit_symbol(req.symbol), "side": req.side.capitalize(),
                "orderType": "Market", "qty": format(req.qty.normalize(), "f"), "marketUnit": "baseCoin",
                "orderLinkId": req.client_order_id, "isLeverage": 0, "timeInForce": "IOC"}

    async def place_order(self, req: OrderRequest) -> OrderState:
        self._require_trading()
        body = self.order_body(req)
        payload = await self._auth().post("/v5/order/create", body)
        code = int(payload.get("retCode", -1))
        if code == 0:
            res = payload.get("result") or {}
            return OrderState(req.client_order_id, OrderStatus.OPEN, qty=req.qty, exchange_order_id=res.get("orderId"))
        reason = f"Bybit {code}: {payload.get('retMsg', '')}"
        if code in DEFINITE_REJECTS:
            return OrderState(req.client_order_id, OrderStatus.REJECTED, qty=req.qty, reject_reason=reason)
        # Anything else: we cannot be sure the order was not created. The caller checks status first.
        raise BrokerError(reason)

    async def cancel_order(self, client_order_id: str) -> OrderState:
        self._require_trading()
        payload = await self._auth().post("/v5/order/cancel", {"category": "spot", "symbol": self.bsym,
                                                               "orderLinkId": client_order_id})
        code = int(payload.get("retCode", -1))
        if code not in (0, *NOT_FOUND_CODES):
            raise BrokerError(f"Bybit {code}: {payload.get('retMsg', '')}")
        return await self.get_order_status(client_order_id)
