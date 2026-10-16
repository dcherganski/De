"""Read-only views for the dashboard. The API never talks to an exchange: it reads what the engine wrote.

Decimals are returned as strings so no amount is rounded through a float on the way to the browser.
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..core.config import Settings
from ..core.jsonutil import jsonable
from ..core.models import (
    AuditEntry,
    BotState,
    ControlCommand,
    CostEntry,
    EngineStatus,
    EquitySnapshot,
    Fill,
    JevCall,
    LatencyEvent,
    NewsItem,
    Order,
    QuoteSnapshot,
    RiskDecision,
    Signal,
)
from ..core.timeutil import age_s, start_of_day
from ..exchange_adapters.registry import SUPPORTED_SYMBOLS, venue_statuses
from ..market_data.types import Quote
from ..paper_broker.broker import PaperBroker
from ..portfolio.service import PortfolioService
from ..strategy.outcomes import calibration
from ..strategy.questions import QUESTION_TEMPLATE_VERSION
from .deps import get_settings_dep, get_sf, now, require_user

router = APIRouter(prefix="/api", tags=["read"], dependencies=[Depends(require_user)])

ENGINE_STALE_S = 15
QUOTE_NOTE = ("Котировка от {venue} (bid/ask на борсата за данни). Не е цена на изпълнение в Robinhood или "
              "друга борса; в режим PAPER сделките се симулират по нея.")
FIXTURE_NOTE = "ТЕСТОВИ ДАННИ (fixture) — не са живи пазарни цени."


def latest_quote(session: Session, s: Settings) -> Quote | None:
    row = session.scalar(select(QuoteSnapshot).where(QuoteSnapshot.symbol == s.symbol)
                         .order_by(QuoteSnapshot.ts_received.desc()).limit(1))
    if row is None:
        return None
    return Quote(row.venue, row.symbol, row.bid, row.ask, row.ts_received, row.bid_qty, row.ask_qty, row.last,
                 row.ts_exchange, row.is_fixture)


def quote_view(q: Quote | None, s: Settings, t) -> dict | None:
    if q is None:
        return None
    age = age_s(q.ts_received, t)
    return {"venue": q.venue, "symbol": q.symbol, "bid": q.bid, "ask": q.ask, "mid": q.mid, "spread_pct": q.spread_pct,
            "received_at": q.ts_received, "age_s": age, "stale": age is None or age > s.max_quote_age_s,
            "is_fixture": q.is_fixture,
            "note": FIXTURE_NOTE if q.is_fixture else QUOTE_NOTE.format(venue=q.venue)}


def signal_view(sig: Signal, t) -> dict:
    return {
        "id": sig.id, "created_at": sig.created_at, "age_s": age_s(sig.created_at, t), "trigger": sig.trigger,
        "trigger_ref": sig.trigger_ref, "venue": sig.venue, "symbol": sig.symbol, "action": sig.action,
        "valid": sig.valid, "reason": sig.reason, "p0_mid": sig.p0_mid, "target_pct": sig.target_pct,
        "stop_pct": sig.stop_pct, "horizon_end": sig.horizon_end, "action_probs": sig.action_probs,
        "action_confidence": sig.action_confidence, "p_close_above": sig.p_close_above, "p_touch": sig.p_touch,
        "p_close_below": sig.p_close_below, "config_version": sig.config_version, "jev_call_id": sig.jev_call_id,
        "outcome": None if sig.outcome_recorded_at is None else {
            "mid": sig.outcome_mid, "close_above": sig.outcome_close_above, "touch": sig.outcome_touch,
            "close_below": sig.outcome_close_below, "recorded_at": sig.outcome_recorded_at, "note": sig.outcome_note},
    }


def risk_view(rd: RiskDecision) -> dict:
    return {"id": rd.id, "created_at": rd.created_at, "signal_id": rd.signal_id, "intent": rd.intent,
            "allowed": rd.allowed, "qty": rd.qty, "reasons": rd.reasons, "context": rd.context}


def order_view(o: Order) -> dict:
    return {"id": o.id, "client_order_id": o.client_order_id, "venue": o.venue, "mode": o.mode, "symbol": o.symbol,
            "side": o.side, "type": o.order_type, "qty": o.qty, "status": o.status, "filled_qty": o.filled_qty,
            "avg_price": o.avg_price, "fee_quote": o.fee_quote, "intent": o.intent, "signal_id": o.signal_id,
            "lot_id": o.lot_id, "created_at": o.created_at, "submitted_at": o.submitted_at, "ack_at": o.ack_at,
            "done_at": o.done_at, "error": o.error}


def live_block_reasons(s: Settings) -> list[str]:
    reasons = []
    if s.mode != "live":
        reasons.append("Режимът е PAPER (по подразбиране).")
    if not s.allow_live_trading:
        reasons.append("ALLOW_LIVE_TRADING не е включено.")
    if not any(v.live_trading_verified for v in venue_statuses(s)):
        reasons.append("Няма адаптер с проверена реална търговия в тази версия.")
    reasons.append("Реална търговия се включва само с твое изрично разрешение.")
    return reasons


@router.get("/status")
def status(request: Request):
    s, sf, t = get_settings_dep(request), get_sf(request), now(request)
    with sf() as session:
        st = session.get(EngineStatus, 1)
        bot = session.get(BotState, 1)
        q = latest_quote(session, s)
        last_sig = session.scalar(select(Signal).order_by(Signal.id.desc()).limit(1))
        blocked = session.scalars(select(RiskDecision).where(RiskDecision.allowed.is_(False))
                                  .order_by(RiskDecision.id.desc()).limit(5)).all()
        pending = session.scalars(select(ControlCommand).where(ControlCommand.status == "pending")).all()
        open_orders = session.scalar(select(func.count(Order.id)).where(
            Order.status.in_(("new", "open", "partially_filled", "unknown")))) or 0
    hb_age = age_s(st.heartbeat_at, t) if st and st.heartbeat_at else None
    engine = None
    if st is not None:
        engine = {
            "running": hb_age is not None and hb_age <= ENGINE_STALE_S, "heartbeat_age_s": hb_age,
            "started_at": st.started_at, "feed_venue": st.feed_venue, "feed_connected": st.feed_connected,
            "is_fixture": st.is_fixture, "last_quote_age_s": age_s(st.last_quote_at, t),
            "last_trade_age_s": age_s(st.last_trade_at, t), "last_candle_open": st.last_candle_close_at,
            "data_ok": st.data_ok, "data_block_reason": st.data_block_reason, "jev_ok": st.jev_ok,
            "last_jev_age_s": age_s(st.last_jev_at, t), "news_ok": st.news_ok,
            "last_news_poll_age_s": age_s(st.last_news_poll_at, t), "last_error": st.last_error,
        }
    return jsonable({
        "now": t,
        "mode": s.mode.upper(),
        "live": {"enabled": False, "blocked_because": live_block_reasons(s)},
        "engine": engine,
        "bot": None if bot is None else {"entries_paused": bot.entries_paused, "paused_reason": bot.paused_reason,
                                         "selected_venue": bot.selected_venue, "selected_symbol": bot.selected_symbol,
                                         "updated_at": bot.updated_at, "updated_by": bot.updated_by},
        "quote": quote_view(q, s, t),
        "last_signal": signal_view(last_sig, t) if last_sig else None,
        "recent_blocks": [risk_view(r) for r in blocked],
        "open_orders": open_orders,
        "pending_commands": [{"id": c.id, "kind": c.kind, "created_at": c.created_at} for c in pending],
        "jev": {"configured": s.typesafe_api_key is not None, "model": s.jev_model},
        "news_provider": s.news_provider,
    })


@router.get("/portfolio")
def portfolio(request: Request):
    s, sf, t = get_settings_dep(request), get_sf(request), now(request)
    svc = PortfolioService(sf, s, account="paper", clock=lambda: t)
    with sf() as session:
        q = latest_quote(session, s)
        summary = svc.summary(session, q)
    summary["quote"] = quote_view(q, s, t)
    summary["simulation_assumptions"] = PaperBroker(sf, s, lambda: None).assumptions()
    summary["valuation_note"] = ("Стойността е по bid (какво би донесла продажба преди такси). Сравнението е със "
                                 "задържане на началния BTC, оценен по същия начин.")
    return jsonable(summary)


@router.get("/equity")
def equity(request: Request, hours: int = Query(24, ge=1, le=24 * 30), points: int = Query(300, ge=10, le=2000)):
    sf, t = get_sf(request), now(request)
    with sf() as session:
        rows = session.scalars(select(EquitySnapshot).where(EquitySnapshot.ts >= t - timedelta(hours=hours))
                               .order_by(EquitySnapshot.ts)).all()
    step = max(1, len(rows) // points)
    picked = rows[::step]
    if rows and picked[-1] is not rows[-1]:
        picked.append(rows[-1])
    return jsonable([{"ts": r.ts, "equity_eur": r.equity_eur, "benchmark_eur": r.benchmark_eur,
                      "drawdown_pct": r.drawdown_pct, "mark_bid": r.mark_bid} for r in picked])


@router.get("/signals")
def signals(request: Request, limit: int = Query(50, ge=1, le=500)):
    sf, t = get_sf(request), now(request)
    with sf() as session:
        rows = session.scalars(select(Signal).order_by(Signal.id.desc()).limit(limit)).all()
    return jsonable([signal_view(r, t) for r in rows])


@router.get("/orders")
def orders(request: Request, limit: int = Query(50, ge=1, le=500)):
    sf = get_sf(request)
    with sf() as session:
        rows = session.scalars(select(Order).order_by(Order.id.desc()).limit(limit)).all()
        fills = session.scalars(select(Fill).where(Fill.order_id.in_([o.id for o in rows])).order_by(Fill.id)).all()
    by_order: dict[int, list] = {}
    for f in fills:
        by_order.setdefault(f.order_id, []).append({"qty": f.qty, "price": f.price, "fee_quote": f.fee_quote,
                                                    "ts": f.ts, "venue_fill_id": f.venue_fill_id})
    return jsonable([order_view(o) | {"fills": by_order.get(o.id, [])} for o in rows])


@router.get("/risk-decisions")
def risk_decisions(request: Request, limit: int = Query(50, ge=1, le=500), blocked_only: bool = False):
    sf = get_sf(request)
    stmt = select(RiskDecision).order_by(RiskDecision.id.desc()).limit(limit)
    if blocked_only:
        stmt = stmt.where(RiskDecision.allowed.is_(False))
    with sf() as session:
        rows = session.scalars(stmt).all()
    return jsonable([risk_view(r) for r in rows])


@router.get("/news")
def news(request: Request, limit: int = Query(50, ge=1, le=300)):
    sf = get_sf(request)
    with sf() as session:
        rows = session.scalars(select(NewsItem).order_by(NewsItem.received_at.desc()).limit(limit)).all()
    return jsonable([{"id": n.id, "provider": n.provider, "source": n.source, "url": n.url, "title": n.title,
                      "published_at": n.published_at, "received_at": n.received_at, "latency_s": n.latency_s,
                      "relevant": n.relevant, "is_fixture": n.is_fixture, "triggered_signal_id": n.reeval_signal_id}
                     for n in rows])


@router.get("/costs")
def costs(request: Request):
    sf, t = get_sf(request), now(request)
    with sf() as session:
        total = dict(session.execute(select(CostEntry.category, func.sum(CostEntry.amount_eur))
                                     .group_by(CostEntry.category)).all())
        today = dict(session.execute(select(CostEntry.category, func.sum(CostEntry.amount_eur))
                                     .where(CostEntry.ts >= start_of_day(t)).group_by(CostEntry.category)).all())
        fees = session.scalar(select(func.coalesce(func.sum(Order.fee_quote), 0)))
        jev = session.execute(select(JevCall.status, func.count(JevCall.id), func.coalesce(func.sum(JevCall.input_tokens), 0))
                              .group_by(JevCall.status)).all()
    return jsonable({
        "total_eur": {k: total.get(k, 0) for k in ("jev", "news", "infra")},
        "today_eur": {k: today.get(k, 0) for k in ("jev", "news", "infra")},
        "trading_fees_eur": fees,
        "jev_calls": [{"status": st, "count": n, "input_tokens": tok} for st, n, tok in jev],
        "notes": ["Таксите за търговия са симулирани по PAPER_FEE_RATE.",
                  "Jev се таксува в USD и се превръща в EUR по курса на ЕЦБ (или резервния курс, ако ЕЦБ не отговаря).",
                  "Инфраструктурата и платените новини са месечни суми от конфигурацията, разпределени по дни."],
    })


@router.get("/latency")
def latency(request: Request, hours: int = Query(24, ge=1, le=24 * 7)):
    sf, t = get_sf(request), now(request)
    ms = LatencyEvent.ms
    with sf() as session:
        rows = session.execute(
            select(LatencyEvent.stage, func.count(LatencyEvent.id), func.percentile_cont(0.5).within_group(ms),
                   func.percentile_cont(0.95).within_group(ms), func.max(ms))
            .where(LatencyEvent.ts >= t - timedelta(hours=hours)).group_by(LatencyEvent.stage)
        ).all()
    return jsonable({"hours": hours, "stages": [{"stage": st, "n": n, "p50_ms": p50, "p95_ms": p95, "max_ms": mx}
                                                for st, n, p50, p95, mx in rows],
                     "note": "Измерени времена в тази инсталация. Не са обещание за скорост на изпълнение."})


@router.get("/calibration")
def calibration_view(request: Request):
    return jsonable(calibration(get_sf(request)) | {
        "explanation": "Brier: средна квадратна грешка на вероятностите (по-ниско е по-добре). Сравнява се с "
                       "постоянна прогноза, равна на наблюдаваната честота. Увереността на Jev не е доказана точност."})


@router.get("/venues")
def venues(request: Request):
    s, sf = get_settings_dep(request), get_sf(request)
    with sf() as session:
        bot = session.get(BotState, 1)
        st = session.get(EngineStatus, 1)
    return jsonable({
        "venues": venue_statuses(s),
        "symbols": SUPPORTED_SYMBOLS,
        "selected_venue": bot.selected_venue if bot else s.quote_venue,
        "selected_symbol": bot.selected_symbol if bot else s.symbol,
        "active_feed": st.feed_venue if st else None,
        "note": ("Изборът на борса сменя източника на цени (и в бъдеще брокера). Не прехвърля средства: "
                 "балансите в различните борси са отделни сметки, а симулираният портфейл остава същият."),
    })


@router.get("/settings")
def settings_view(request: Request):
    s = get_settings_dep(request)
    limits = ("max_order_eur max_exposure_eur daily_loss_limit_eur max_trades_per_day min_seconds_between_trades "
              "max_spread_pct max_quote_age_s max_candle_gap_s pause_blocks_exits").split()
    strategy = ("horizon_min target_margin_pct stop_pct buy_choice_min buy_close_above_min sell_choice_min "
                "sell_close_below_min signal_ttl_s take_profit_pct max_hold_min").split()
    return jsonable({
        "mode": s.mode.upper(), "symbol": s.symbol, "quote_venue": s.quote_venue,
        "risk_limits": {k: getattr(s, k) for k in limits},
        "strategy": {k: getattr(s, k) for k in strategy},
        "config_version": s.config_version(QUESTION_TEMPLATE_VERSION),
        "question_template_version": QUESTION_TEMPLATE_VERSION,
        "jev": {"model": s.jev_model, "configured": s.typesafe_api_key is not None, "timeout_s": s.jev_timeout_s,
                "price_usd_per_mtok_input": s.jev_price_usd_per_mtok},
        "news": {"provider": s.news_provider, "feeds": s.rss_feeds if s.news_provider == "rss" else []},
        "secrets": {"credentials_storage": s.secrets_master_key is not None},
        "note": "Праговете са експериментални настройки, не доказано печеливши стойности. Промяна: .env + рестарт.",
    })


@router.get("/audit")
def audit_view(request: Request, limit: int = Query(100, ge=1, le=1000), kind: str | None = None):
    sf = get_sf(request)
    stmt = select(AuditEntry).order_by(AuditEntry.id.desc()).limit(limit)
    if kind:
        stmt = stmt.where(AuditEntry.kind == kind)
    with sf() as session:
        rows = session.scalars(stmt).all()
    return jsonable([{"id": a.id, "ts": a.ts, "actor": a.actor, "kind": a.kind, "ref": a.ref, "payload": a.payload}
                     for a in rows])
