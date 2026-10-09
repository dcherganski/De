"""Strategy: ask Jev at a closed 5-minute candle (and, rate limited, after significant news), turn
the answer into BUY/HOLD/SELL with thresholds in code, and record everything for later scoring.

Jev does not see the future. Its numbers are probabilities from a language model reading the state;
they are logged with the realised outcome 30 minutes later so their calibration can be checked.
Confidence is never treated as proven accuracy.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import and_, select

from ..audit import log as audit
from ..core.config import Settings
from ..core.fx import FxProvider
from ..core.jsonutil import jsonable
from ..core.models import Candle, JevCall, NewsItem, Order, Signal
from ..core.timeutil import utcnow
from ..features.compute import compute_features
from ..jev_client.client import JevClient, JevError
from ..jev_client.schemas import ChoiceAnswer, NoulAnswer, SystemOneRequest
from .outcomes import record_outcomes
from .questions import QUESTION_TEMPLATE_VERSION, build_questions
from .state import ago, build_state, costs, target_pct

log = logging.getLogger(__name__)
CANDLES_FOR_FEATURES = 60
NEWS_WINDOW = timedelta(hours=3)


def decide(s: Settings, answers: dict) -> tuple[str, str]:
    """BUY/HOLD/SELL from Jev's probabilities. Every threshold is an experimental setting."""
    action: ChoiceAnswer = answers["action"]
    p_above = answers["close_above_target"].noul
    p_below = answers["close_below_stop"].noul
    probs = action.probabilities
    if action.choice == "BUY":
        if probs["BUY"] >= float(s.buy_choice_min) and p_above >= float(s.buy_close_above_min):
            return "BUY", f"BUY {probs['BUY']:.2f} ≥ {s.buy_choice_min}; над целта {p_above:.2f} ≥ {s.buy_close_above_min}"
        return "HOLD", (f"Jev предлага BUY, но праговете не са изпълнени (BUY {probs['BUY']:.2f}, "
                        f"над целта {p_above:.2f})")
    if action.choice == "SELL":
        if probs["SELL"] >= float(s.sell_choice_min) and p_below >= float(s.sell_close_below_min):
            return "SELL", f"SELL {probs['SELL']:.2f} ≥ {s.sell_choice_min}; под стопа {p_below:.2f} ≥ {s.sell_close_below_min}"
        return "HOLD", (f"Jev предлага SELL, но праговете не са изпълнени (SELL {probs['SELL']:.2f}, "
                        f"под стопа {p_below:.2f})")
    return "HOLD", f"Jev предлага HOLD ({probs.get('HOLD', 0):.2f})"


class StrategyRunner:
    def __init__(self, settings: Settings, sf, jev: JevClient, fx: FxProvider, clock=None):
        self.s = settings
        self.sf = sf
        self.jev = jev
        self.fx = fx
        self.clock = clock
        self.jev_ok: bool | None = None
        self.last_jev_at = None
        self._last_news_eval = None
        self._last_outcome_check = None

    @classmethod
    def from_settings(cls, s: Settings, sf) -> StrategyRunner:
        key = s.typesafe_api_key.get_secret_value() if s.typesafe_api_key else None
        jev = JevClient(key, s.typesafe_base_url, s.jev_timeout_s, s.jev_max_retries)
        return cls(s, sf, jev, FxProvider(s.usd_per_eur_fallback))

    def _now(self, engine):
        return engine.clock() if engine is not None else (self.clock or utcnow)()

    # ------------------------------------------------------------------ inputs
    def _candles(self, session, venue: str):
        rows = session.scalars(
            select(Candle).where(Candle.venue == venue, Candle.symbol == self.s.symbol, Candle.interval_s == 300,
                                 Candle.closed.is_(True)).order_by(Candle.open_time.desc()).limit(CANDLES_FOR_FEATURES)
        ).all()
        from ..market_data.types import CandleBar
        return [CandleBar(r.venue, r.symbol, r.interval_s, r.open_time, r.open, r.high, r.low, r.close, r.volume,
                          r.trade_count, True, r.is_fixture) for r in reversed(rows)]

    def _news(self, session, now) -> list[NewsItem]:
        return list(session.scalars(
            select(NewsItem).where(NewsItem.relevant.is_(True), NewsItem.received_at >= now - NEWS_WINDOW)
            .order_by(NewsItem.received_at.desc()).limit(self.s.news_items_in_state)
        ))

    def _recent_trades(self, session, now) -> list[dict]:
        rows = session.scalars(select(Order).where(Order.filled_qty > 0).order_by(Order.created_at.desc()).limit(5))
        return [{"side": o.side, "reason": o.intent, "when": ago(o.created_at, now), "qty_btc": str(o.filled_qty),
                 "avg_price": str(o.avg_price)} for o in rows]

    # ------------------------------------------------------------------ evaluation
    async def evaluate(self, engine, trig) -> int | None:
        now = self._now(engine)
        check = engine.market.check(self.s.max_quote_age_s, self.s.max_candle_gap_s, now)
        if not check.ok:
            with self.sf.begin() as session:
                audit.record(session, "strategy", "evaluation_skipped", trig.ref,
                             {"trigger": trig.kind, "reasons": check.reasons})
            return None  # no Jev call (and no cost) on stale or missing data
        q = engine.market.quote
        with self.sf() as session:
            candles = self._candles(session, engine.feed.venue)
            news_rows = self._news(session, now)
            cash_free, _, _, base_total = engine.portfolio.holdings(session)
            trades = self._recent_trades(session, now)
        feats = compute_features(candles, q)
        cost = costs(q, self.s.paper_fee_rate, self.s.paper_slippage_bps)
        tgt = target_pct(cost["round_trip_cost_pct"], self.s.target_margin_pct)
        news = [{"source": n.source, "published": ago(n.published_at or n.received_at, now), "title": n.title,
                 "summary": n.summary} for n in news_rows]
        state = build_state(symbol=self.s.symbol, quote=q, now=now, horizon_min=self.s.horizon_min, features=feats,
                            cost=cost, tgt_pct=tgt, stop_pct=self.s.stop_pct, cash_free=cash_free,
                            base_total=base_total, recent_trades=trades, news=news)
        req = SystemOneRequest(state=state, model=self.s.jev_model, questions=build_questions(self.s.horizon_min))
        config_version = self.s.config_version(QUESTION_TEMPLATE_VERSION)
        with self.sf.begin() as session:
            call = JevCall(created_at=now, trigger=trig.kind, model_requested=self.s.jev_model,
                           config_version=config_version, request_json=jsonable(req.payload()), status="pending")
            session.add(call)
            session.flush()
            call_id = call.id
        signal_kw = dict(created_at=now, trigger=trig.kind, trigger_ref=trig.ref, jev_call_id=call_id, venue=q.venue,
                         symbol=self.s.symbol, p0_bid=q.bid, p0_ask=q.ask, p0_mid=q.mid, target_pct=tgt,
                         stop_pct=self.s.stop_pct, horizon_end=now + timedelta(minutes=self.s.horizon_min),
                         config_version=config_version)
        try:
            res = await self.jev.ask(req)
        except JevError as exc:
            self.jev_ok = None if exc.status == "skipped" else False  # None: not configured
            with self.sf.begin() as session:
                call = session.get(JevCall, call_id)
                call.status, call.error = exc.status, str(exc)[:1000]
                sig = Signal(action="NONE", valid=False, reason=f"няма валиден отговор от Jev ({exc.status}): {exc}"[:500],
                             **signal_kw)
                session.add(sig)
                session.flush()
                audit.record(session, "strategy", "jev_failed", str(call_id), {"status": exc.status, "error": str(exc)})
                self._mark_news(session, trig, sig.id)
                return sig.id
        self.jev_ok, self.last_jev_at = True, now
        answers = res.response.answers
        action, reason = decide(self.s, answers)
        cost_usd = res.cost_usd(self.s.jev_price_usd_per_mtok)
        cost_eur, fx = await self.fx.usd_to_eur(cost_usd)
        act: ChoiceAnswer = answers["action"]
        p_touch: NoulAnswer = answers["touch_target"]
        with self.sf.begin() as session:
            call = session.get(JevCall, call_id)
            call.status, call.model_returned = "ok", res.response.model
            call.response_json = res.raw
            call.latency_ms = res.latency_ms
            call.input_tokens, call.output_tokens = res.response.usage.input_tokens, res.response.usage.output_tokens
            call.cost_usd, call.cost_eur = cost_usd, cost_eur
            audit.cost(session, "jev", cost_usd, "USD", cost_eur, fx.usd_per_eur, fx.source, f"jev_call:{call_id}", now)
            audit.latency(session, "jev_call", res.latency_ms, str(call_id), now)
            sig = Signal(action=action, valid=True, reason=reason, action_probs=act.probabilities,
                         action_confidence=Decimal(str(act.confidence)),
                         p_close_above=Decimal(str(answers["close_above_target"].noul)),
                         p_touch=Decimal(str(p_touch.noul)), p_close_below=Decimal(str(answers["close_below_stop"].noul)),
                         **signal_kw)
            session.add(sig)
            session.flush()
            audit.record(session, "strategy", "signal", str(sig.id),
                         {"action": action, "reason": reason, "model": res.response.model, "jev_call_id": call_id,
                          "trigger": trig.kind})
            self._mark_news(session, trig, sig.id)
            return sig.id

    def _mark_news(self, session, trig, signal_id: int) -> None:
        if trig.kind == "news" and trig.news_ids:
            for n in session.scalars(select(NewsItem).where(NewsItem.id.in_(trig.news_ids))):
                n.reeval_signal_id = signal_id

    # ------------------------------------------------------------------ news trigger
    def news_trigger(self, engine, new_ids: list[int]):
        """At most one news re-evaluation per interval; each item can trigger only once."""
        from ..engine.runner import Trigger
        now = self._now(engine)
        if self._last_news_eval is not None and (now - self._last_news_eval).total_seconds() < self.s.news_reeval_min_interval_s:
            return None
        cutoff = now - timedelta(seconds=self.s.news_max_age_for_reeval_s)
        with self.sf() as session:
            fresh = list(session.scalars(select(NewsItem.id).where(
                NewsItem.id.in_(new_ids), NewsItem.relevant.is_(True), NewsItem.reeval_signal_id.is_(None),
                and_(NewsItem.published_at.is_not(None), NewsItem.published_at >= cutoff))))
        if not fresh:
            return None
        self._last_news_eval = now
        return Trigger("news", "news:" + ",".join(map(str, fresh[:10])), now, news_ids=tuple(fresh))

    async def periodic(self, engine) -> None:
        now = self._now(engine)
        if self._last_outcome_check is None or (now - self._last_outcome_check).total_seconds() >= 60:
            self._last_outcome_check = now
            record_outcomes(self.sf, now)
