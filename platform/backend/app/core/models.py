"""Relational schema. Money and quantities are NUMERIC(38,18); every timestamp is timestamptz in UTC.

`paper_*` tables are the simulated exchange's own books. They are kept apart from the platform's
`orders`/`fills`/`balances` so that reconciliation after a restart works the same way it would
against a real venue: the platform asks the broker, it does not read its own notes.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .db import Base

TS = DateTime(timezone=True)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS)


class UserSession(Base):
    __tablename__ = "user_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    created_at: Mapped[datetime] = mapped_column(TS)
    expires_at: Mapped[datetime] = mapped_column(TS)


class ExchangeCredential(Base):
    __tablename__ = "exchange_credentials"
    __table_args__ = (UniqueConstraint("venue", "label"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    venue: Mapped[str] = mapped_column(String(32))
    label: Mapped[str] = mapped_column(String(64))
    blob: Mapped[bytes] = mapped_column(LargeBinary)  # Fernet ciphertext, never sent to the browser
    key_hint: Mapped[str] = mapped_column(String(16))
    permissions_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(TS)


class QuoteSnapshot(Base):
    __tablename__ = "quote_snapshots"
    __table_args__ = (Index("ix_quote_symbol_ts", "symbol", "ts_received"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    venue: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    bid: Mapped[Decimal]
    ask: Mapped[Decimal]
    bid_qty: Mapped[Decimal | None]
    ask_qty: Mapped[Decimal | None]
    last: Mapped[Decimal | None]
    ts_exchange: Mapped[datetime | None] = mapped_column(TS)
    ts_received: Mapped[datetime] = mapped_column(TS)
    is_fixture: Mapped[bool] = mapped_column(Boolean, default=False)


class Candle(Base):
    __tablename__ = "candles"
    venue: Mapped[str] = mapped_column(String(32), primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), primary_key=True)
    interval_s: Mapped[int] = mapped_column(Integer, primary_key=True)
    open_time: Mapped[datetime] = mapped_column(TS, primary_key=True)
    open: Mapped[Decimal]
    high: Mapped[Decimal]
    low: Mapped[Decimal]
    close: Mapped[Decimal]
    volume: Mapped[Decimal]
    trade_count: Mapped[int] = mapped_column(Integer, default=0)
    closed: Mapped[bool] = mapped_column(Boolean, default=False)
    is_fixture: Mapped[bool] = mapped_column(Boolean, default=False)


class NewsItem(Base):
    __tablename__ = "news_items"
    __table_args__ = (Index("ix_news_title_hash", "title_hash"), Index("ix_news_received", "received_at"))
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    provider: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(String(128))
    url: Mapped[str] = mapped_column(Text)
    url_norm: Mapped[str] = mapped_column(Text, unique=True)
    title: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(Text, default="")
    title_hash: Mapped[str] = mapped_column(String(64))
    published_at: Mapped[datetime | None] = mapped_column(TS)
    received_at: Mapped[datetime] = mapped_column(TS)
    latency_s: Mapped[Decimal | None]
    relevant: Mapped[bool] = mapped_column(Boolean, default=False)
    is_fixture: Mapped[bool] = mapped_column(Boolean, default=False)
    reeval_signal_id: Mapped[int | None] = mapped_column(BigInteger)


class JevCall(Base):
    __tablename__ = "jev_calls"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TS)
    trigger: Mapped[str] = mapped_column(String(32))
    model_requested: Mapped[str] = mapped_column(String(64))
    model_returned: Mapped[str | None] = mapped_column(String(64))
    config_version: Mapped[str] = mapped_column(String(32))
    request_json: Mapped[dict] = mapped_column(JSONB)
    response_json: Mapped[dict | None] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(16))  # ok | error | invalid | timeout | skipped
    error: Mapped[str | None] = mapped_column(Text)
    latency_ms: Mapped[int | None] = mapped_column(Integer)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[Decimal | None]
    cost_eur: Mapped[Decimal | None]


class Signal(Base):
    __tablename__ = "signals"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TS)
    trigger: Mapped[str] = mapped_column(String(32))  # candle_close | news
    trigger_ref: Mapped[str | None] = mapped_column(String(128))
    jev_call_id: Mapped[int | None] = mapped_column(ForeignKey("jev_calls.id"))
    venue: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    p0_bid: Mapped[Decimal]
    p0_ask: Mapped[Decimal]
    p0_mid: Mapped[Decimal]
    target_pct: Mapped[Decimal]
    stop_pct: Mapped[Decimal]
    horizon_end: Mapped[datetime] = mapped_column(TS)
    action: Mapped[str] = mapped_column(String(8))  # BUY | HOLD | SELL | NONE
    action_probs: Mapped[dict | None] = mapped_column(JSONB)
    action_confidence: Mapped[Decimal | None]
    p_close_above: Mapped[Decimal | None]
    p_touch: Mapped[Decimal | None]
    p_close_below: Mapped[Decimal | None]
    valid: Mapped[bool] = mapped_column(Boolean)
    reason: Mapped[str] = mapped_column(Text, default="")
    config_version: Mapped[str] = mapped_column(String(32))
    outcome_mid: Mapped[Decimal | None]
    outcome_close_above: Mapped[bool | None] = mapped_column(Boolean)
    outcome_touch: Mapped[bool | None] = mapped_column(Boolean)
    outcome_close_below: Mapped[bool | None] = mapped_column(Boolean)
    outcome_recorded_at: Mapped[datetime | None] = mapped_column(TS)
    outcome_note: Mapped[str | None] = mapped_column(Text)


class RiskDecision(Base):
    __tablename__ = "risk_decisions"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TS)
    signal_id: Mapped[int | None] = mapped_column(ForeignKey("signals.id"))
    intent: Mapped[str] = mapped_column(String(16))
    allowed: Mapped[bool] = mapped_column(Boolean)
    qty: Mapped[Decimal]
    reasons: Mapped[list] = mapped_column(JSONB)
    context: Mapped[dict] = mapped_column(JSONB)


class Lot(Base):
    __tablename__ = "lots"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    opened_at: Mapped[datetime] = mapped_column(TS)
    entry_order_id: Mapped[int | None] = mapped_column(BigInteger)
    qty_open: Mapped[Decimal]
    entry_price: Mapped[Decimal]  # average fill price; fees are tracked on the order
    target_price: Mapped[Decimal | None]
    stop_price: Mapped[Decimal | None]
    expires_at: Mapped[datetime | None] = mapped_column(TS)
    status: Mapped[str] = mapped_column(String(16), default="open")
    closed_at: Mapped[datetime | None] = mapped_column(TS)
    exit_reason: Mapped[str | None] = mapped_column(String(32))
    initial: Mapped[bool] = mapped_column(Boolean, default=False)


class Order(Base):
    __tablename__ = "orders"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    venue: Mapped[str] = mapped_column(String(32))
    mode: Mapped[str] = mapped_column(String(8))
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(4))
    order_type: Mapped[str] = mapped_column(String(16), default="market")
    qty: Mapped[Decimal]
    status: Mapped[str] = mapped_column(String(20))
    filled_qty: Mapped[Decimal] = mapped_column(default=Decimal(0))
    avg_price: Mapped[Decimal | None]
    fee_quote: Mapped[Decimal] = mapped_column(default=Decimal(0))
    exchange_order_id: Mapped[str | None] = mapped_column(String(64))
    intent: Mapped[str] = mapped_column(String(16))
    signal_id: Mapped[int | None] = mapped_column(BigInteger)
    risk_decision_id: Mapped[int | None] = mapped_column(BigInteger)
    lot_id: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(TS)
    updated_at: Mapped[datetime] = mapped_column(TS)
    submitted_at: Mapped[datetime | None] = mapped_column(TS)
    ack_at: Mapped[datetime | None] = mapped_column(TS)
    done_at: Mapped[datetime | None] = mapped_column(TS)
    error: Mapped[str | None] = mapped_column(Text)


class Fill(Base):
    __tablename__ = "fills"
    __table_args__ = (UniqueConstraint("venue", "venue_fill_id"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey("orders.id"))
    venue: Mapped[str] = mapped_column(String(32))
    venue_fill_id: Mapped[str] = mapped_column(String(64))
    qty: Mapped[Decimal]
    price: Mapped[Decimal]
    fee_quote: Mapped[Decimal]
    ts: Mapped[datetime] = mapped_column(TS)


class Balance(Base):
    __tablename__ = "balances"
    account: Mapped[str] = mapped_column(String(32), primary_key=True)
    asset: Mapped[str] = mapped_column(String(16), primary_key=True)
    free: Mapped[Decimal]
    locked: Mapped[Decimal] = mapped_column(default=Decimal(0))
    updated_at: Mapped[datetime] = mapped_column(TS)


class PortfolioInit(Base):
    __tablename__ = "portfolio_init"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TS)
    venue: Mapped[str] = mapped_column(String(32))
    symbol: Mapped[str] = mapped_column(String(32))
    bid: Mapped[Decimal]
    ask: Mapped[Decimal]
    mid: Mapped[Decimal]
    base_qty: Mapped[Decimal]
    value_eur: Mapped[Decimal]
    cash_eur: Mapped[Decimal]
    quote_ts: Mapped[datetime] = mapped_column(TS)
    is_fixture: Mapped[bool] = mapped_column(Boolean, default=False)


class EquitySnapshot(Base):
    __tablename__ = "equity_snapshots"
    ts: Mapped[datetime] = mapped_column(TS, primary_key=True)
    cash: Mapped[Decimal]
    base_qty: Mapped[Decimal]
    mark_bid: Mapped[Decimal]
    equity_eur: Mapped[Decimal]
    benchmark_eur: Mapped[Decimal]
    peak_equity_eur: Mapped[Decimal]
    drawdown_pct: Mapped[Decimal]
    fees_total_eur: Mapped[Decimal]


class BotState(Base):
    __tablename__ = "bot_state"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entries_paused: Mapped[bool] = mapped_column(Boolean, default=False)
    paused_reason: Mapped[str | None] = mapped_column(Text)
    selected_venue: Mapped[str] = mapped_column(String(32))
    selected_symbol: Mapped[str] = mapped_column(String(32))
    updated_at: Mapped[datetime] = mapped_column(TS)
    updated_by: Mapped[str] = mapped_column(String(64))


class ControlCommand(Base):
    __tablename__ = "control_commands"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(TS)
    kind: Mapped[str] = mapped_column(String(32))  # cancel_pending | sell_position
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)
    requested_by: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16), default="pending")
    processed_at: Mapped[datetime | None] = mapped_column(TS)
    result: Mapped[str | None] = mapped_column(Text)


class EngineStatus(Base):
    __tablename__ = "engine_status"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    started_at: Mapped[datetime | None] = mapped_column(TS)
    heartbeat_at: Mapped[datetime | None] = mapped_column(TS)
    feed_venue: Mapped[str | None] = mapped_column(String(32))
    feed_connected: Mapped[bool] = mapped_column(Boolean, default=False)
    is_fixture: Mapped[bool] = mapped_column(Boolean, default=False)
    last_quote_at: Mapped[datetime | None] = mapped_column(TS)
    last_trade_at: Mapped[datetime | None] = mapped_column(TS)
    last_candle_close_at: Mapped[datetime | None] = mapped_column(TS)
    news_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_news_poll_at: Mapped[datetime | None] = mapped_column(TS)
    jev_ok: Mapped[bool | None] = mapped_column(Boolean)
    last_jev_at: Mapped[datetime | None] = mapped_column(TS)
    data_ok: Mapped[bool] = mapped_column(Boolean, default=False)
    data_block_reason: Mapped[str | None] = mapped_column(Text)
    last_error: Mapped[str | None] = mapped_column(Text)


class AuditEntry(Base):
    """Append-only: the migration installs a trigger that rejects UPDATE and DELETE."""

    __tablename__ = "audit_log"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS, index=True)
    actor: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(48), index=True)
    ref: Mapped[str | None] = mapped_column(String(128))
    payload: Mapped[dict] = mapped_column(JSONB, default=dict)


class LatencyEvent(Base):
    __tablename__ = "latency_events"
    __table_args__ = (Index("ix_latency_stage_ts", "stage", "ts"),)
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS)
    stage: Mapped[str] = mapped_column(String(32))
    ms: Mapped[Decimal]
    ref: Mapped[str | None] = mapped_column(String(128))


class CostEntry(Base):
    __tablename__ = "cost_entries"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(TS, index=True)
    category: Mapped[str] = mapped_column(String(16))  # jev | news | infra | trading_fee
    amount: Mapped[Decimal]
    currency: Mapped[str] = mapped_column(String(8))
    amount_eur: Mapped[Decimal]
    fx_rate: Mapped[Decimal | None]
    fx_source: Mapped[str | None] = mapped_column(String(64))
    ref: Mapped[str | None] = mapped_column(String(128))


class PaperOrder(Base):
    __tablename__ = "paper_orders"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    client_order_id: Mapped[str] = mapped_column(String(64), unique=True)
    symbol: Mapped[str] = mapped_column(String(32))
    side: Mapped[str] = mapped_column(String(4))
    qty: Mapped[Decimal]
    filled_qty: Mapped[Decimal] = mapped_column(default=Decimal(0))
    avg_price: Mapped[Decimal | None]
    fee_quote: Mapped[Decimal] = mapped_column(default=Decimal(0))
    locked_quote: Mapped[Decimal] = mapped_column(default=Decimal(0))
    status: Mapped[str] = mapped_column(String(20))  # open | partially_filled | filled | cancelled | rejected
    reject_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(TS)
    updated_at: Mapped[datetime] = mapped_column(TS)


class PaperFill(Base):
    __tablename__ = "paper_fills"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    paper_order_id: Mapped[int] = mapped_column(ForeignKey("paper_orders.id"))
    qty: Mapped[Decimal]
    price: Mapped[Decimal]
    fee_quote: Mapped[Decimal]
    ts: Mapped[datetime] = mapped_column(TS)
    quote_bid: Mapped[Decimal]
    quote_ask: Mapped[Decimal]


class PaperBalance(Base):
    __tablename__ = "paper_balances"
    asset: Mapped[str] = mapped_column(String(16), primary_key=True)
    free: Mapped[Decimal]
    locked: Mapped[Decimal] = mapped_column(default=Decimal(0))
    updated_at: Mapped[datetime] = mapped_column(TS)
