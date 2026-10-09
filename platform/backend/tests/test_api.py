"""API: login, CSRF, read views, controls, credentials and the LIVE refusal, on SYNTHETIC engine data."""

import json
from datetime import timedelta
from decimal import Decimal

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import func, select

from app.api import auth
from app.api.main import create_app
from app.api.security import COOKIE, hash_password
from app.audit.costs import accrue_fixed_costs
from app.core.models import AuditEntry, BotState, ControlCommand, CostEntry, ExchangeCredential, Order, User
from app.core.secrets import decrypt_credentials
from app.engine.runner import Engine, EngineRestart
from app.paper_broker.broker import PaperBroker

from .conftest import T0, synthetic_events
from .test_engine_flow import ListFeed, run_engine

PW = "correct horse battery staple"
CSRF = {"X-Requested-With": "platform"}
FAKE_API_KEY = "TESTKEY-not-real-0000-abcd"
FAKE_API_SECRET = "TESTSECRET-not-real-zzzz-9999"


class FixedClock:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


def make_client(sf, settings, clock):
    auth.throttle._fails.clear()
    with sf.begin() as s:
        if s.scalar(select(User).where(User.username == "ana")) is None:
            s.add(User(username="ana", password_hash=hash_password(PW), created_at=T0))
    return TestClient(create_app(settings, sf, clock=clock))


def login(client):
    r = client.post("/api/auth/login", json={"username": "ana", "password": PW}, headers=CSRF)
    assert r.status_code == 200, r.text
    client.headers.update(CSRF)
    return r


@pytest.fixture
def clock():
    return FixedClock(T0 + timedelta(minutes=30))


@pytest.fixture
def client(sf, settings, clock):
    return make_client(sf, settings, clock)


def test_auth_required_and_csrf(client, sf):
    assert client.get("/api/health").json() == {"ok": True}
    assert client.get("/api/status").status_code == 401
    assert client.post("/api/auth/login", json={"username": "ana", "password": PW}).status_code == 403
    bad = client.post("/api/auth/login", json={"username": "ana", "password": "wrong password!!"}, headers=CSRF)
    assert bad.status_code == 401
    r = client.post("/api/auth/login", json={"username": "ana", "password": PW}, headers=CSRF)
    cookie = r.headers["set-cookie"]
    assert r.status_code == 200 and "HttpOnly" in cookie and "SameSite=strict" in cookie
    assert client.get("/api/auth/me").json() == {"username": "ana"}
    assert client.post("/api/control/pause", json={}).status_code == 403  # logged in, but no CSRF header
    with sf() as s:
        kinds = [a.kind for a in s.scalars(select(AuditEntry).order_by(AuditEntry.id))]
        assert "login_failed" in kinds and "login" in kinds
        assert PW not in json.dumps([a.payload for a in s.scalars(select(AuditEntry))])
    assert client.post("/api/auth/logout", headers=CSRF).status_code == 200
    assert client.get("/api/auth/me").status_code == 401


def test_login_throttle(client):
    for _ in range(5):
        client.post("/api/auth/login", json={"username": "ana", "password": "nope nope nope"}, headers=CSRF)
    r = client.post("/api/auth/login", json={"username": "ana", "password": PW}, headers=CSRF)
    assert r.status_code == 429


def test_session_expiry(sf, settings, clock):
    client = make_client(sf, settings, clock)
    login(client)
    clock.now += timedelta(hours=settings.session_ttl_h, seconds=1)
    assert client.get("/api/status").status_code == 401


async def test_read_views_after_engine_run(sf, settings, clock):
    await run_engine(sf, settings, synthetic_events(minutes=21))
    client = make_client(sf, settings, clock)
    clock.now = T0 + timedelta(minutes=21)
    login(client)
    st = client.get("/api/status").json()
    assert st["mode"] == "PAPER" and st["live"]["enabled"] is False
    assert any("изрично разрешение" in r for r in st["live"]["blocked_because"])
    assert st["quote"]["is_fixture"] and "ТЕСТОВИ ДАННИ" in st["quote"]["note"]
    assert st["jev"]["configured"] is False
    pf = client.get("/api/portfolio").json()
    assert pf["initialized"] and pf["init"]["is_fixture"]
    assert Decimal(pf["init"]["value_eur"]) <= Decimal("200") and Decimal(pf["cash_total"]) == 0
    assert isinstance(pf["equity_eur"], str)  # exact decimals, not floats
    assert pf["simulation_assumptions"]["fee_rate"] == "0.0040"
    assert set(pf["costs_eur"]) == {"jev", "news", "infra"}
    eq = client.get("/api/equity?hours=24&points=10").json()
    assert eq and len(eq) <= 12
    lat = client.get("/api/latency").json()
    assert "quote_receive" in {s["stage"] for s in lat["stages"]}
    assert client.get("/api/calibration").json()["n"] == 0
    for path in ("/api/signals", "/api/orders", "/api/risk-decisions", "/api/news", "/api/costs", "/api/audit",
                 "/api/settings", "/api/venues", "/api/control/commands"):
        assert client.get(path).status_code == 200, path
    settings_view = client.get("/api/settings").json()
    assert "typesafe_api_key" not in json.dumps(settings_view)


async def test_controls_pause_sell_and_duplicates(sf, settings, clock):
    engine = await run_engine(sf, settings, synthetic_events(minutes=6))
    client = make_client(sf, settings, clock)
    login(client)
    r = client.post("/api/control/pause", json={"reason": "test"})
    assert r.status_code == 200 and "Нищо не е продадено" in r.json()["note"]
    with sf() as s:
        assert s.get(BotState, 1).entries_paused
        assert s.scalar(select(func.count(Order.id))) == 0  # pausing never sells
    bad = client.post("/api/control/sell-position", json={"confirm": False})
    assert bad.status_code == 422 and "input" not in json.dumps(bad.json())
    first = client.post("/api/control/sell-position", json={"confirm": True}).json()
    second = client.post("/api/control/sell-position", json={"confirm": True}).json()
    assert not first["duplicate"] and second["duplicate"] and first["command_id"] == second["command_id"]
    await engine.process_commands()
    with sf() as s:
        cmd = s.get(ControlCommand, first["command_id"])
        assert cmd.status == "done"
        orders = s.scalars(select(Order)).all()
        assert len(orders) == 1 and orders[0].intent == "MANUAL_SELL" and orders[0].side == "sell"
    assert client.post("/api/control/resume").json() == {"entries_paused": False}
    assert client.post("/api/control/cancel-pending").json()["status"] == "pending"


async def test_live_mode_refused_and_venue_choice(sf, settings, clock):
    await run_engine(sf, settings, synthetic_events(minutes=2))
    client = make_client(sf, settings, clock)
    login(client)
    r = client.post("/api/control/mode", json={"mode": "live"})
    assert r.status_code == 409 and r.json()["detail"]["reasons"]
    assert client.post("/api/control/mode", json={"mode": "paper"}).json() == {"mode": "PAPER"}
    venues = client.get("/api/venues").json()
    rh = next(v for v in venues["venues"] if v["name"] == "robinhood")
    assert rh["available"] is False and "САЩ" in rh["reason"] and rh["checked_on"] == "2026-10-09"
    assert "Не прехвърля средства" in venues["note"]
    assert client.post("/api/control/venue", json={"venue": "robinhood"}).status_code == 422
    assert client.post("/api/control/venue", json={"venue": "kraken", "symbol": "ETH/EUR"}).status_code == 422
    ok = client.post("/api/control/venue", json={"venue": "kraken", "symbol": "BTC/EUR"})
    assert ok.status_code == 200 and "Не са прехвърлени средства" in ok.json()["note"]
    with sf() as s:
        assert s.get(BotState, 1).selected_venue == "kraken"
        refused = s.scalar(select(AuditEntry).where(AuditEntry.kind == "live_mode_refused"))
        assert refused.actor == "ana"
        venue = s.scalar(select(AuditEntry).where(AuditEntry.kind == "control_venue"))
        assert venue.payload["funds_moved"] is False


async def test_engine_restarts_on_new_selection(sf, settings):
    class LiveLikeFeed(ListFeed):
        venue = "kraken"
        is_fixture = False

    broker = PaperBroker(sf, settings, lambda: None)
    engine = Engine(settings, sf, LiveLikeFeed([]), broker)
    engine.ensure_rows()
    engine.check_selection()  # unchanged selection: keeps running
    with sf.begin() as s:
        s.get(BotState, 1).selected_venue = "coinbase"
    with pytest.raises(EngineRestart):
        engine.check_selection()


def test_credentials_encrypted_never_returned(sf, settings, clock):
    client = make_client(sf, settings, clock)
    login(client)
    body = {"venue": "kraken", "label": "main", "api_key": FAKE_API_KEY, "api_secret": FAKE_API_SECRET,
            "permissions_note": "Query Funds, Create & Modify Orders; без теглене", "confirm_no_withdrawal": True}
    assert client.post("/api/credentials", json=body).status_code == 503  # no master key configured

    key = Fernet.generate_key().decode()
    s2 = settings.model_copy(update={"secrets_master_key": SecretStr(key)})
    client = make_client(sf, s2, clock)
    login(client)
    r = client.post("/api/credentials", json=body)
    assert r.status_code == 200 and r.json()["key_hint"] == "…abcd"
    listing = client.get("/api/credentials")
    for resp in (r, listing):
        assert FAKE_API_KEY not in resp.text and FAKE_API_SECRET not in resp.text
    short = client.post("/api/credentials", json=body | {"api_key": "SHORT7!"})
    assert short.status_code == 422 and "SHORT7!" not in short.text
    assert client.post("/api/credentials", json=body | {"venue": "robinhood"}).status_code == 422
    assert client.post("/api/credentials", json=body | {"confirm_no_withdrawal": False}).status_code == 422
    with sf() as s:
        row = s.scalar(select(ExchangeCredential))
        assert FAKE_API_SECRET.encode() not in row.blob
        assert decrypt_credentials(s2, row.blob) == {"api_key": FAKE_API_KEY, "api_secret": FAKE_API_SECRET}
        dump = json.dumps([a.payload for a in s.scalars(select(AuditEntry))])
        assert FAKE_API_KEY not in dump and FAKE_API_SECRET not in dump
    assert client.delete(f"/api/credentials/{row.id}").json() == {"deleted": row.id}
    assert client.get("/api/credentials").json()["credentials"] == []


async def test_credentials_never_reach_jev(sf, settings):
    from .test_strategy import jev_body, make_strategy

    key = Fernet.generate_key().decode()
    s2 = settings.model_copy(update={"secrets_master_key": SecretStr(key)})
    client = make_client(sf, s2, FixedClock(T0))
    login(client)
    client.post("/api/credentials", json={"venue": "kraken", "label": "main", "api_key": FAKE_API_KEY,
                                          "api_secret": FAKE_API_SECRET, "confirm_no_withdrawal": True})
    strategy, seen = make_strategy(sf, s2, [jev_body("HOLD")] * 3)
    await run_engine(sf, s2, synthetic_events(minutes=11), strategy)
    assert seen
    blob = json.dumps(seen)
    assert FAKE_API_KEY not in blob and FAKE_API_SECRET not in blob and "…abcd" not in blob


def test_fixed_costs_accrue_once_per_day(sf, settings):
    s2 = settings.model_copy(update={"infra_cost_eur_per_month": Decimal("30"), "news_cost_eur_per_month": Decimal("0")})
    assert accrue_fixed_costs(sf, s2, T0) == 1
    assert accrue_fixed_costs(sf, s2, T0 + timedelta(hours=5)) == 0
    assert accrue_fixed_costs(sf, s2, T0 + timedelta(days=1)) == 1
    with sf() as s:
        rows = s.scalars(select(CostEntry).where(CostEntry.category == "infra")).all()
        assert len(rows) == 2 and all(r.amount_eur == Decimal(1) for r in rows)
        assert s.scalar(select(func.count(CostEntry.id)).where(CostEntry.category == "news")) == 0


def test_session_cookie_is_not_the_stored_value(client, sf):
    r = client.post("/api/auth/login", json={"username": "ana", "password": PW}, headers=CSRF)
    token = r.cookies.get(COOKIE) or client.cookies.get(COOKIE)
    from app.core.models import UserSession

    with sf() as s:
        stored = [u.token_hash for u in s.scalars(select(UserSession))]
    assert token and token not in stored and len(stored[0]) == 64
