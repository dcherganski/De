"""Controls and exchange credentials. Every action is written to the audit log with the user's name.

The API never places orders itself. It flips the pause flag or queues a command; the engine process
executes commands through the same risk checks as everything else. Stopping new orders never sells.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, SecretStr
from sqlalchemy import select

from ..audit import log as audit
from ..core.jsonutil import jsonable
from ..core.models import BotState, ControlCommand, ExchangeCredential, User
from ..core.secrets import SecretsUnavailable, decrypt_credentials, encrypt_credentials, mask
from ..exchange_adapters.base import BrokerError
from ..exchange_adapters.bybit import BybitAdapter
from ..exchange_adapters.registry import SUPPORTED_SYMBOLS, selectable_venues
from ..market_data.bybit import BybitError
from .deps import CurrentUser, get_settings_dep, get_sf, now
from .read import live_block_reasons

router = APIRouter(prefix="/api", tags=["control"])


def _bot(session, t, user: str) -> BotState:
    bot = session.get(BotState, 1, with_for_update=True)
    if bot is None:
        raise HTTPException(409, "Двигателят още не е стартирал (няма състояние на бота).")
    bot.updated_at, bot.updated_by = t, user
    return bot


class PauseIn(BaseModel):
    reason: str = Field("", max_length=200)


@router.post("/control/pause")
def pause(body: PauseIn, request: Request, user: CurrentUser):
    """„Спри новите поръчки“: no new entries. Holdings stay as they are."""
    t = now(request)
    with get_sf(request).begin() as session:
        bot = _bot(session, t, user.username)
        bot.entries_paused, bot.paused_reason = True, body.reason or "спряно от таблото"
        audit.record(session, user.username, "control_pause", None, {"reason": bot.paused_reason}, ts=t)
    blocks_exits = get_settings_dep(request).pause_blocks_exits
    return {"entries_paused": True,
            "note": "Новите поръчки са спрени. Нищо не е продадено." +
                    (" Автоматичните изходи също чакат; „Продай позицията“ остава достъпно." if blocks_exits else "")}


@router.post("/control/resume")
def resume(request: Request, user: CurrentUser):
    t = now(request)
    with get_sf(request).begin() as session:
        bot = _bot(session, t, user.username)
        bot.entries_paused, bot.paused_reason = False, None
        audit.record(session, user.username, "control_resume", None, {}, ts=t)
    return {"entries_paused": False}


def _queue(request: Request, user: User, kind: str, payload: dict) -> dict:
    t = now(request)
    with get_sf(request).begin() as session:
        existing = session.scalar(select(ControlCommand).where(ControlCommand.kind == kind,
                                                               ControlCommand.status == "pending"))
        if existing is not None:  # a double click must not become two sell orders
            return {"command_id": existing.id, "status": "pending", "duplicate": True}
        cmd = ControlCommand(created_at=t, kind=kind, payload=payload, requested_by=user.username, status="pending")
        session.add(cmd)
        session.flush()
        audit.record(session, user.username, f"control_{kind}", str(cmd.id), payload, ts=t)
        return {"command_id": cmd.id, "status": "pending", "duplicate": False}


@router.post("/control/cancel-pending")
def cancel_pending(request: Request, user: CurrentUser):
    """„Отмени чакащите поръчки“: the engine cancels open orders at the broker."""
    return _queue(request, user, "cancel_pending", {})


class SellIn(BaseModel):
    confirm: Literal[True]


@router.post("/control/sell-position")
def sell_position(body: SellIn, request: Request, user: CurrentUser):
    """„Продай позицията“: market sell of all free BTC, the starting BTC included. Needs confirm=true."""
    out = _queue(request, user, "sell_position", {"confirmed": True})
    out["note"] = ("Пазарна продажба на целия свободен BTC по текущия bid. Цената не е гарантирана; "
                   "двигателят пак проверява свежестта на котировката и спреда.")
    return out


@router.get("/control/commands")
def commands(request: Request, _: CurrentUser):
    with get_sf(request)() as session:
        rows = session.scalars(select(ControlCommand).order_by(ControlCommand.id.desc()).limit(50)).all()
    return jsonable([{"id": c.id, "kind": c.kind, "status": c.status, "requested_by": c.requested_by,
                      "created_at": c.created_at, "processed_at": c.processed_at, "result": c.result} for c in rows])


class VenueIn(BaseModel):
    venue: str = Field(max_length=32)
    symbol: str = Field("BTC/EUR", max_length=32)


@router.post("/control/venue")
def select_venue(body: VenueIn, request: Request, user: CurrentUser):
    s, t = get_settings_dep(request), now(request)
    allowed = selectable_venues(s)
    if body.venue not in allowed:
        raise HTTPException(422, f"Борсата „{body.venue}“ не може да се избере в тази версия. Достъпни: {', '.join(allowed)}.")
    if not any(p["symbol"] == body.symbol and p["supported"] for p in SUPPORTED_SYMBOLS):
        raise HTTPException(422, f"Двойката {body.symbol} не се поддържа.")
    with get_sf(request).begin() as session:
        bot = _bot(session, t, user.username)
        before = (bot.selected_venue, bot.selected_symbol)
        bot.selected_venue, bot.selected_symbol = body.venue, body.symbol
        audit.record(session, user.username, "control_venue", None,
                     {"from": list(before), "to": [body.venue, body.symbol], "funds_moved": False}, ts=t)
    return {"selected_venue": body.venue, "selected_symbol": body.symbol,
            "note": ("Сменен е източникът на цени. Не са прехвърлени средства. Двигателят се рестартира сам "
                     "до няколко секунди и продължава със същия симулиран портфейл.")}


class ModeIn(BaseModel):
    mode: Literal["paper", "live"]


@router.post("/control/mode")
def set_mode(body: ModeIn, request: Request, user: CurrentUser):
    s, t = get_settings_dep(request), now(request)
    if body.mode == "paper":
        return {"mode": s.mode.upper()}
    with get_sf(request).begin() as session:
        audit.record(session, user.username, "live_mode_refused", None, {"reasons": live_block_reasons(s)}, ts=t)
    raise HTTPException(409, {"message": "LIVE не може да се включи.", "reasons": live_block_reasons(s)})


# ---------------------------------------------------------------- exchange credentials
class CredentialIn(BaseModel):
    venue: Literal["bybit", "kraken"]
    label: str = Field(min_length=1, max_length=64, pattern=r"^[\w .-]+$")
    api_key: SecretStr = Field(min_length=8, max_length=512)
    api_secret: SecretStr = Field(min_length=8, max_length=4096)
    permissions_note: str = Field("", max_length=500)
    confirm_no_withdrawal: Literal[True]


def _cred_view(c: ExchangeCredential) -> dict:
    return {"id": c.id, "venue": c.venue, "label": c.label, "key_hint": c.key_hint,
            "permissions_note": c.permissions_note, "created_at": c.created_at}


@router.get("/credentials")
def list_credentials(request: Request, _: CurrentUser):
    with get_sf(request)() as session:
        rows = session.scalars(select(ExchangeCredential).order_by(ExchangeCredential.id)).all()
    return jsonable({"credentials": [_cred_view(c) for c in rows],
                     "storage_enabled": get_settings_dep(request).secrets_master_key is not None,
                     "note": "Ключовете се пазят криптирани на сървъра и никога не се връщат към браузъра."})


@router.post("/credentials")
def store_credentials(body: CredentialIn, request: Request, user: CurrentUser):
    s, t = get_settings_dep(request), now(request)
    try:
        blob = encrypt_credentials(s, {"api_key": body.api_key.get_secret_value(),
                                       "api_secret": body.api_secret.get_secret_value()})
    except SecretsUnavailable as exc:
        raise HTTPException(503, "SECRETS_MASTER_KEY не е зададен на сървъра; ключове не могат да се пазят.") from exc
    hint = mask(body.api_key.get_secret_value())
    with get_sf(request).begin() as session:
        row = session.scalar(select(ExchangeCredential).where(ExchangeCredential.venue == body.venue,
                                                              ExchangeCredential.label == body.label))
        if row is None:
            row = ExchangeCredential(venue=body.venue, label=body.label, created_at=t)
            session.add(row)
        row.blob, row.key_hint, row.permissions_note, row.created_at = blob, hint, body.permissions_note, t
        session.flush()
        audit.record(session, user.username, "credential_stored", str(row.id),
                     {"venue": body.venue, "label": body.label, "key_hint": hint, "no_withdrawal_confirmed": True}, ts=t)
        view = _cred_view(row)
    view["note"] = "Ключът е запазен криптиран. Търговия с него остава заключена, докато LIVE не е разрешен."
    return jsonable(view)


@router.delete("/credentials/{cred_id}")
def delete_credentials(cred_id: int, request: Request, user: CurrentUser):
    t = now(request)
    with get_sf(request).begin() as session:
        row = session.get(ExchangeCredential, cred_id)
        if row is None:
            raise HTTPException(404, "Няма такъв ключ.")
        audit.record(session, user.username, "credential_deleted", str(cred_id),
                     {"venue": row.venue, "label": row.label, "key_hint": row.key_hint}, ts=t)
        session.delete(row)
    return {"deleted": cred_id}


@router.post("/credentials/{cred_id}/check")
async def check_credentials(cred_id: int, request: Request, user: CurrentUser):
    """Ask the exchange which permissions the stored key has. Read-only; refuses withdrawal rights."""
    s, t = get_settings_dep(request), now(request)
    with get_sf(request)() as session:
        row = session.get(ExchangeCredential, cred_id)
        if row is None:
            raise HTTPException(404, "Няма такъв ключ.")
        venue, blob, hint = row.venue, row.blob, row.key_hint
    if venue != "bybit":
        raise HTTPException(422, f"Проверка на права не е налична за {venue} в тази версия.")
    try:
        creds = decrypt_credentials(s, blob)
    except SecretsUnavailable as exc:
        raise HTTPException(503, str(exc)) from exc
    adapter = BybitAdapter(s.symbol, region=s.bybit_region, credentials=creds,
                           transport=request.app.state.http_transport)
    try:
        result = await adapter.check_key()
    except (BrokerError, BybitError) as exc:
        result = {"ok": False, "problems": [f"проверката не успя: {exc}"]}
    with get_sf(request).begin() as session:
        audit.record(session, user.username, "credential_checked", str(cred_id),
                     {"venue": venue, "key_hint": hint, "region": s.bybit_region, **result}, ts=t)
    return jsonable(result | {"region": s.bybit_region})
