"""Request-scoped dependencies: settings, DB, clock, the logged-in user and the CSRF check."""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.orm import Session, sessionmaker

from ..core.config import Settings
from ..core.models import User, UserSession
from ..core.timeutil import ensure_utc
from .security import COOKIE, CSRF_HEADER, CSRF_VALUE, token_hash

MUTATING = ("POST", "PUT", "PATCH", "DELETE")


def get_settings_dep(request: Request) -> Settings:
    return request.app.state.settings


def get_sf(request: Request) -> sessionmaker[Session]:
    return request.app.state.sf


def now(request: Request):
    return request.app.state.clock()


def csrf(request: Request) -> None:
    """SameSite=Strict already stops cross-site cookies; the custom header is a second, independent guard."""
    if request.method in MUTATING and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
        raise HTTPException(403, "Липсва заглавката X-Requested-With: platform (защита от CSRF).")


def require_user(request: Request, _: Annotated[None, Depends(csrf)]) -> User:
    token = request.cookies.get(COOKIE)
    if not token:
        raise HTTPException(401, "Не сте влезли.")
    sf = get_sf(request)
    with sf() as session:
        row = session.get(UserSession, token_hash(token))
        if row is None or ensure_utc(row.expires_at) <= now(request):
            raise HTTPException(401, "Сесията е изтекла. Влезте отново.")
        user = session.get(User, row.user_id)
    if user is None:
        raise HTTPException(401, "Потребителят не съществува.")
    return user


CurrentUser = Annotated[User, Depends(require_user)]
