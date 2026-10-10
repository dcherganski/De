// Shapes returned by the FastAPI backend. Money and quantities arrive as decimal strings.
export type Dec = string;

export interface QuoteView {
  venue: string;
  symbol: string;
  bid: Dec;
  ask: Dec;
  mid: Dec;
  spread_pct: Dec;
  received_at: string;
  age_s: number | null;
  stale: boolean;
  is_fixture: boolean;
  note: string;
}

export interface SignalView {
  id: number;
  created_at: string;
  age_s: number | null;
  trigger: string;
  trigger_ref: string | null;
  venue: string;
  symbol: string;
  action: "BUY" | "HOLD" | "SELL" | "NONE";
  valid: boolean;
  reason: string;
  p0_mid: Dec;
  target_pct: Dec;
  stop_pct: Dec;
  horizon_end: string;
  action_probs: Record<string, number> | null;
  action_confidence: Dec | null;
  p_close_above: Dec | null;
  p_touch: Dec | null;
  p_close_below: Dec | null;
  config_version: string;
  outcome: null | {
    mid: Dec | null;
    close_above: boolean | null;
    touch: boolean | null;
    close_below: boolean | null;
    recorded_at: string;
    note: string | null;
  };
}

export interface RiskView {
  id: number;
  created_at: string;
  signal_id: number | null;
  intent: string;
  allowed: boolean;
  qty: Dec;
  reasons: string[];
  context: Record<string, unknown>;
}

export interface EngineView {
  running: boolean;
  heartbeat_age_s: number | null;
  started_at: string | null;
  feed_venue: string | null;
  feed_connected: boolean;
  is_fixture: boolean;
  last_quote_age_s: number | null;
  last_trade_age_s: number | null;
  data_ok: boolean;
  data_block_reason: string | null;
  jev_ok: boolean | null;
  last_jev_age_s: number | null;
  news_ok: boolean | null;
  last_news_poll_age_s: number | null;
  last_error: string | null;
}

export interface Status {
  now: string;
  mode: "PAPER" | "LIVE";
  live: { enabled: boolean; blocked_because: string[] };
  engine: EngineView | null;
  bot: null | {
    entries_paused: boolean;
    paused_reason: string | null;
    selected_venue: string;
    selected_symbol: string;
    updated_at: string;
    updated_by: string;
  };
  quote: QuoteView | null;
  last_signal: SignalView | null;
  recent_blocks: RiskView[];
  open_orders: number;
  pending_commands: { id: number; kind: string; created_at: string }[];
  jev: { configured: boolean; model: string };
  news_provider: string;
}

export interface Portfolio {
  initialized: boolean;
  base_asset: string;
  quote_asset: string;
  cash_free: Dec;
  cash_total: Dec;
  base_free: Dec;
  base_total: Dec;
  fees_trading_eur: Dec;
  costs_eur: { jev: Dec; news: Dec; infra: Dec };
  max_drawdown_pct: Dec;
  init?: { created_at: string; venue: string; mid: Dec; base_qty: Dec; value_eur: Dec; cash_eur: Dec; is_fixture: boolean };
  mark_bid?: Dec;
  equity_eur?: Dec;
  benchmark_hold_eur?: Dec;
  pnl_vs_start_eur?: Dec;
  pnl_vs_start_after_analysis_costs_eur?: Dec;
  pnl_vs_hold_eur?: Dec;
  quote: QuoteView | null;
  simulation_assumptions: {
    fee_rate: Dec;
    slippage_bps: Dec;
    min_qty: Dec;
    qty_step: Dec;
    min_cost: Dec;
    partial_fills: boolean;
    top_of_book_participation: Dec;
    notes: string[];
  };
  valuation_note: string;
}

export interface EquityPoint {
  ts: string;
  equity_eur: Dec;
  benchmark_eur: Dec;
  drawdown_pct: Dec;
}

export interface OrderView {
  id: number;
  client_order_id: string;
  venue: string;
  mode: string;
  side: "buy" | "sell";
  qty: Dec;
  status: string;
  filled_qty: Dec;
  avg_price: Dec | null;
  fee_quote: Dec;
  intent: string;
  created_at: string;
  error: string | null;
  fills: { qty: Dec; price: Dec; fee_quote: Dec; ts: string }[];
}

export interface NewsView {
  id: number;
  source: string;
  url: string;
  title: string;
  published_at: string | null;
  received_at: string;
  latency_s: Dec | null;
  relevant: boolean;
  is_fixture: boolean;
  triggered_signal_id: number | null;
}

export interface Costs {
  total_eur: Record<"jev" | "news" | "infra", Dec | number>;
  today_eur: Record<"jev" | "news" | "infra", Dec | number>;
  trading_fees_eur: Dec;
  jev_calls: { status: string; count: number; input_tokens: number }[];
  notes: string[];
}

export interface Latency {
  hours: number;
  stages: { stage: string; n: number; p50_ms: number; p95_ms: number; max_ms: Dec }[];
  note: string;
}

export interface CalibrationBin {
  lo: number;
  hi: number;
  n: number;
  hits: number;
  mean_forecast: number;
  observed_rate: number;
  ci_lo: number;
  ci_hi: number;
}

export interface CalibrationEntry {
  n: number;
  mean_forecast: number;
  observed_rate: number;
  brier: number;
  brier_of_base_rate: number;
  bins: CalibrationBin[];
}

export interface Calibration {
  n: number;
  close_above: CalibrationEntry | null;
  touch: CalibrationEntry | null;
  close_below: CalibrationEntry | null;
  note: string;
  explanation: string;
}

export interface VenueStatus {
  name: string;
  available: boolean;
  market_data: boolean;
  trading: boolean;
  live_trading_verified: boolean;
  reason: string;
  docs: string[];
  checked_on: string;
}

export interface Venues {
  venues: VenueStatus[];
  symbols: { symbol: string; supported: boolean; note: string }[];
  selected_venue: string;
  selected_symbol: string;
  active_feed: string | null;
  note: string;
}

export interface SettingsView {
  mode: string;
  symbol: string;
  risk_limits: Record<string, string | number | boolean>;
  strategy: Record<string, string | number | null>;
  config_version: string;
  jev: { model: string; configured: boolean };
  news: { provider: string; feeds: string[] };
  secrets: { credentials_storage: boolean };
  note: string;
}

export interface Credential {
  id: number;
  venue: string;
  label: string;
  key_hint: string;
  permissions_note: string;
  created_at: string;
}
