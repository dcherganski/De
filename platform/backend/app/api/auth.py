"""Login and logout. There is no sign-up: the single user is created with `python -m app.cli create-user`."""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from ..audit import log as audit
from ..core.models import User, UserSession
from .deps import CurrentUser, csrf, get_settings_dep, get_sf, now
from .security import COOKIE, DUMMY_HASH, LoginThrottle, new_token, token_hash, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])
throttle = LoginThrottle()


class LoginIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


@router.post("/login", dependencies=[Depends(csrf)])
def login(body: LoginIn, request: Request, response: Response):
    s = get_settings_dep(request)
    sf = get_sf(request)
    client = request.client.host if request.client else "?"
    keys = (f"user:{body.username}", f"ip:{client}")
    if any(throttle.blocked(k) for k in keys):
        raise HTTPException(429, "Твърде много неуспешни опити. Опитайте след няколко минути.")
    t = now(request)
    with sf() as session:
        user = session.scalar(select(User).where(User.username == body.username))
    ok = verify_password(user.password_hash if user else DUMMY_HASH, body.password) and user is not None
    if not ok:
        for k in keys:
            throttle.fail(k)
        with sf.begin() as session:  # committed before the error response, so the attempt stays on record
            audit.record(session, "api", "login_failed", None, {"username": body.username, "client": client}, ts=t)
        raise HTTPException(401, "Грешно потребителско име или парола.")
    with sf.begin() as session:
        for k in keys:
            throttle.reset(k)
        session.execute(delete(UserSession).where(UserSession.expires_at <= t))
        token, digest = new_token()
        expires = t + timedelta(hours=s.session_ttl_h)
        session.add(UserSession(token_hash=digest, user_id=user.id, created_at=t, expires_at=expires))
        audit.record(session, user.username, "login", None, {"client": client}, ts=t)
    response.set_cookie(COOKIE, token, max_age=s.session_ttl_h * 3600, httponly=True, samesite="strict",
                        secure=s.cookie_secure, path="/")
    return {"username": user.username, "expires_at": expires.isoformat()}


@router.post("/logout")
def logout(request: Request, response: Response, user: CurrentUser):
    token = request.cookies.get(COOKIE, "")
    with get_sf(request).begin() as session:
        session.execute(delete(UserSession).where(UserSession.token_hash == token_hash(token)))
        audit.record(session, user.username, "logout", None, {}, ts=now(request))
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/me")
def me(user: CurrentUser):
    return {"username": user.username}
