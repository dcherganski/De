"""Independent risk check. The strategy proposes; this module decides whether and how much.

It only ever answers with an allowed quantity or a list of reasons. It never sells on its own:
stopping, pausing or a breached limit blocks new orders and leaves the position alone.

Protective exits (take profit, stop, time limit) and the manual "sell position" reduce risk, so they
skip the trade-count, cooldown, daily-loss and spread checks. They still need a fresh quote, still
respect the exchange minimums and the maximum order value (a large position is sold in slices),
and are blocked while paused when `pause_blocks_exits` is on (default), because a pause means
"send nothing automatically".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from ..core.config import Settings
from ..core.numbers import BPS, ZERO, floor_to_step
from ..market_data.types import MarketRules, Quote

ENTRY = "BUY"
REDUCE = "SELL"
EXITS = ("EXIT_TP", "EXIT_SL", "EXIT_TIME")
MANUAL_SELL = "MANUAL_SELL"


@dataclass
class RiskContext:
    now: datetime
    quote: Quote | None
    data_ok: bool
    data_reasons: list[str]
    cash_free: Decimal
    base_free: Decimal
    base_total: Decimal
    open_orders: int
    trades_today: int
    last_trade_at: datetime | None
    day_start_equity: Decimal | None
    equity_now: Decimal | None
    entries_paused: bool
    rules: MarketRules
    signal_age_s: float | None = None
    signal_valid: bool = True


@dataclass
class RiskResult:
    intent: str
    allowed: bool
    qty: Decimal = ZERO
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notional_est: Decimal = ZERO


class RiskEngine:
    def __init__(self, settings: Settings):
        self.s = settings
        self.slip = settings.paper_slippage_bps * BPS
        self.fee = settings.paper_fee_rate

    # ------------------------------------------------------------------ shared checks
    def _common(self, ctx: RiskContext, reasons: list[str], *, automatic: bool, is_exit: bool) -> None:
        if not ctx.data_ok:
            reasons.extend(f"данни: {r}" for r in ctx.data_reasons)
        if ctx.quote is None:
            reasons.append("няма котировка")
        if ctx.open_orders > 0:
            reasons.append("вече има чакаща поръчка")
        if automatic and ctx.entries_paused and (not is_exit or self.s.pause_blocks_exits):
            reasons.append("новите поръчки са спрени от потребителя")

    def _entry_limits(self, ctx: RiskContext, reasons: list[str]) -> None:
        q = ctx.quote
        if q is not None and q.spread_pct > self.s.max_spread_pct:
            reasons.append(f"спредът {q.spread_pct:.3f}% е над лимита {self.s.max_spread_pct}%")
        if ctx.trades_today >= self.s.max_trades_per_day:
            reasons.append(f"дневният лимит от {self.s.max_trades_per_day} сделки е достигнат")
        if ctx.last_trade_at is not None:
            since = (ctx.now - ctx.last_trade_at).total_seconds()
            if since < self.s.min_seconds_between_trades:
                reasons.append(f"пауза между сделките: още {self.s.min_seconds_between_trades - since:.0f} s")
        if ctx.day_start_equity is not None and ctx.equity_now is not None:
            loss = ctx.day_start_equity - ctx.equity_now  # includes unrealized, marked at the bid
            if loss >= self.s.daily_loss_limit_eur:
                reasons.append(f"дневната загуба {loss:.2f} € достига лимита {self.s.daily_loss_limit_eur} €")
        if not ctx.signal_valid:
            reasons.append("сигналът не е валиден")
        if ctx.signal_age_s is not None and ctx.signal_age_s > self.s.signal_ttl_s:
            reasons.append(f"сигналът е на {ctx.signal_age_s:.0f} s (> {self.s.signal_ttl_s} s)")

    # ------------------------------------------------------------------ buy
    def evaluate_buy(self, ctx: RiskContext) -> RiskResult:
        reasons: list[str] = []
        self._common(ctx, reasons, automatic=True, is_exit=False)
        self._entry_limits(ctx, reasons)
        res = RiskResult(intent=ENTRY, allowed=False)
        q = ctx.quote
        if q is None:
            res.reasons = reasons
            return res
        ask_eff = q.ask * (1 + self.slip)
        unit_cost = ask_eff * (1 + self.fee)
        exposure = ctx.base_total * q.bid
        budget = min(self.s.max_order_eur, ctx.cash_free / (1 + self.fee) if ctx.cash_free > ZERO else ZERO,
                     max(ZERO, self.s.max_exposure_eur - exposure))
        if ctx.cash_free <= ZERO:
            reasons.append("няма свободни средства за покупка (0 €)")
        if exposure >= self.s.max_exposure_eur:
            reasons.append(f"експозицията {exposure:.2f} € е на лимита {self.s.max_exposure_eur} €")
        qty = floor_to_step(budget / ask_eff, ctx.rules.qty_step) if budget > ZERO else ZERO
        while qty > ZERO and qty * unit_cost > ctx.cash_free:  # fee rounding can tip it over
            qty -= ctx.rules.qty_step
        if ctx.cash_free > ZERO and qty < ctx.rules.min_qty:
            reasons.append(f"количеството {qty} е под минимума {ctx.rules.min_qty}")
        if ctx.cash_free > ZERO and qty * ask_eff < ctx.rules.min_cost:
            reasons.append(f"стойността е под минималната {ctx.rules.min_cost}")
        res.reasons = reasons
        res.allowed = not reasons
        res.qty = qty if res.allowed else ZERO
        res.notional_est = qty * ask_eff
        return res

    # ------------------------------------------------------------------ sells
    def evaluate_sell(self, ctx: RiskContext, intent: str, qty_wanted: Decimal) -> RiskResult:
        automatic = intent != MANUAL_SELL
        is_exit = intent in EXITS or intent == MANUAL_SELL
        reasons: list[str] = []
        self._common(ctx, reasons, automatic=automatic, is_exit=is_exit)
        if intent == REDUCE:
            self._entry_limits(ctx, reasons)
        res = RiskResult(intent=intent, allowed=False)
        q = ctx.quote
        if q is None:
            res.reasons = reasons
            return res
        if is_exit and q.spread_pct > self.s.max_spread_pct:
            res.warnings.append(f"широк спред {q.spread_pct:.3f}% при защитен изход")
        bid_eff = q.bid * (1 - self.slip)
        qty = min(qty_wanted, ctx.base_free)
        cap = floor_to_step(self.s.max_order_eur / bid_eff, ctx.rules.qty_step)
        if qty > cap:
            qty = cap
            if ctx.base_free - qty < ctx.rules.min_qty:  # do not leave unsellable dust behind
                qty = ctx.base_free
        qty = floor_to_step(qty, ctx.rules.qty_step)
        if qty <= ZERO:
            reasons.append("няма BTC за продажба")
        elif qty < ctx.rules.min_qty:
            reasons.append(f"количеството {qty} е под минимума {ctx.rules.min_qty}")
        elif qty * bid_eff < ctx.rules.min_cost:
            reasons.append(f"стойността е под минималната {ctx.rules.min_cost}")
        res.reasons = reasons
        res.allowed = not reasons
        res.qty = qty if res.allowed else ZERO
        res.notional_est = qty * bid_eff
        return res
