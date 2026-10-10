"use client";

import { useState } from "react";
import { ReliabilityChart } from "@/components/ReliabilityChart";
import type { Calibration, CalibrationEntry, Costs, Latency, Portfolio, SettingsView, SignalView, Status } from "@/lib/types";
import {
  ACTION_LABEL, fmtAge, fmtBtc, fmtEur, fmtPct, fmtPrice, fmtProb, fmtSignedEur, fmtTime, INTENT_LABEL, toNum,
} from "@/lib/format";

function Pill({ ok, children }: { ok: boolean | null | undefined; children: React.ReactNode }) {
  const cls = ok === null || ok === undefined ? "pill" : ok ? "pill ok" : "pill bad";
  return <span className={cls}>{children}</span>;
}

const sign = (v: string | undefined) => (toNum(v) ?? 0) >= 0 ? "pos" : "neg";

export function StatusPanel({ status }: { status: Status }) {
  const e = status.engine;
  const q = status.quote;
  return (
    <section className="card span-12" aria-labelledby="status-h">
      <h2 id="status-h">Връзки и данни</h2>
      <div className="row">
        <Pill ok={e?.running}>{e?.running ? "Двигателят работи" : "Двигателят не отговаря"} ({fmtAge(e?.heartbeat_age_s)})</Pill>
        <Pill ok={e?.feed_connected}>Поток {e?.feed_venue ?? "—"}: {e?.feed_connected ? "свързан" : "няма връзка"}</Pill>
        <Pill ok={e?.data_ok}>{e?.data_ok ? "Данните са свежи" : "Данните не стигат за сделка"}</Pill>
        <Pill ok={status.jev.configured ? e?.jev_ok : false}>
          Jev: {status.jev.configured ? (e?.jev_ok === false ? "грешка" : `${status.jev.model}, ${fmtAge(e?.last_jev_age_s)}`) : "няма ключ"}
        </Pill>
        <Pill ok={status.news_provider === "none" ? null : e?.news_ok}>
          Новини: {status.news_provider === "none" ? "изключени" : `${status.news_provider}, ${fmtAge(e?.last_news_poll_age_s)}`}
        </Pill>
      </div>
      {e && !e.data_ok && e.data_block_reason && <p className="notice">Сделките са блокирани: {e.data_block_reason}</p>}
      {e?.last_error && <p className="notice error">Последна грешка: <span className="mono">{e.last_error}</span></p>}
      {q && (
        <div className="stack" style={{ marginTop: 10 }}>
          <div className="row num">
            <strong>{q.symbol}</strong>
            <span>bid {fmtPrice(q.bid)}</span>
            <span>ask {fmtPrice(q.ask)}</span>
            <span>спред {fmtPct(q.spread_pct, 3)}</span>
            <Pill ok={!q.stale}>{fmtAge(q.age_s)}</Pill>
          </div>
          <small>{q.note}</small>
        </div>
      )}
    </section>
  );
}

export function PortfolioPanel({ p }: { p: Portfolio }) {
  if (!p.initialized) {
    return (
      <section className="card span-12">
        <h2>Портфейл</h2>
        <p className="muted">Чака първата котировка, за да превърне 200 € в BTC по записаната цена.</p>
      </section>
    );
  }
  const costs = p.costs_eur;
  return (
    <section className="card span-12" aria-labelledby="pf-h">
      <h2 id="pf-h">Портфейл (стойност в EUR)</h2>
      <div className="kpis">
        <div className="kpi"><div className="label">Стойност сега</div><div className="value">{fmtEur(p.equity_eur)}</div></div>
        <div className="kpi"><div className="label">BTC</div><div className="value">{fmtBtc(p.base_total)}</div></div>
        <div className="kpi"><div className="label">Свободни EUR</div><div className="value">{fmtEur(p.cash_free)}</div></div>
        <div className="kpi">
          <div className="label">P&L спрямо началото, след разходите за анализ</div>
          <div className={`value ${sign(p.pnl_vs_start_after_analysis_costs_eur)}`}>{fmtSignedEur(p.pnl_vs_start_after_analysis_costs_eur)}</div>
        </div>
        <div className="kpi">
          <div className="label">Спрямо задържане на началния BTC</div>
          <div className={`value ${sign(p.pnl_vs_hold_eur)}`}>{fmtSignedEur(p.pnl_vs_hold_eur)}</div>
        </div>
        <div className="kpi"><div className="label">Максимален спад</div><div className="value">{fmtPct(p.max_drawdown_pct)}</div></div>
        <div className="kpi"><div className="label">Такси за търговия (симулирани)</div><div className="value">{fmtEur(p.fees_trading_eur)}</div></div>
        <div className="kpi">
          <div className="label">Jev / новини / инфраструктура</div>
          <div className="value" style={{ fontSize: "0.95rem" }}>
            {fmtEur(costs.jev, true)} / {fmtEur(costs.news)} / {fmtEur(costs.infra)}
          </div>
        </div>
      </div>
      {p.init && (
        <p><small>
          Начало {fmtTime(p.init.created_at)}: {fmtBtc(p.init.base_qty)} по средна цена {fmtPrice(p.init.mid)} от {p.init.venue}
          {" "}(= {fmtEur(p.init.value_eur)}), свободни {fmtEur(p.init.cash_eur)}.{p.init.is_fixture && " ТЕСТОВИ ДАННИ."} {p.valuation_note}
        </small></p>
      )}
    </section>
  );
}

export function SignalPanel({ status }: { status: Status }) {
  const s = status.last_signal;
  return (
    <section className="card span-6" aria-labelledby="sig-h">
      <h2 id="sig-h">Последен сигнал</h2>
      {!s ? <p className="muted">Още няма сигнал.</p> : <SignalDetail s={s} />}
      <h2 style={{ marginTop: 14 }}>Защо е блокирано</h2>
      {status.recent_blocks.length === 0 ? (
        <p className="muted">Няма блокирани решения.</p>
      ) : (
        <ul className="stack" style={{ paddingLeft: 18, margin: 0 }}>
          {status.recent_blocks.map((r) => (
            <li key={r.id}>
              <small>{fmtTime(r.created_at)} · {INTENT_LABEL[r.intent] ?? r.intent}</small>
              <div>{r.reasons.join("; ")}</div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function SignalDetail({ s }: { s: SignalView }) {
  return (
    <div className="stack">
      <div className="row">
        <span className="mode paper" style={{ fontSize: "0.95rem", padding: "4px 12px" }}>{ACTION_LABEL[s.action] ?? s.action}</span>
        <span>{fmtAge(s.age_s)}</span>
        <span className="pill">{s.trigger === "news" ? "по новина" : "затворена 5-мин свещ"}</span>
        {!s.valid && <span className="pill bad">невалиден</span>}
      </div>
      <table>
        <tbody>
          <tr><th>Цена ≥ +{fmtPct(s.target_pct)} след 30 мин</th><td className="num">{fmtProb(s.p_close_above)}</td></tr>
          <tr><th>Докосва +{fmtPct(s.target_pct)} в рамките на 30 мин</th><td className="num">{fmtProb(s.p_touch)}</td></tr>
          <tr><th>Цена ≤ −{fmtPct(s.stop_pct)} след 30 мин</th><td className="num">{fmtProb(s.p_close_below)}</td></tr>
        </tbody>
      </table>
      {s.reason && <small>{s.reason}</small>}
      <small>Вероятностите са оценка на Jev, не доказана точност. Виж калибрацията по-долу.</small>
    </div>
  );
}

export function CostsPanel({ c }: { c: Costs }) {
  return (
    <section className="card span-4" aria-labelledby="costs-h">
      <h2 id="costs-h">Разходи</h2>
      <table>
        <thead><tr><th></th><th className="num">днес</th><th className="num">общо</th></tr></thead>
        <tbody>
          <tr><th>Jev</th><td className="num">{fmtEur(c.today_eur.jev, true)}</td><td className="num">{fmtEur(c.total_eur.jev, true)}</td></tr>
          <tr><th>Новини</th><td className="num">{fmtEur(c.today_eur.news)}</td><td className="num">{fmtEur(c.total_eur.news)}</td></tr>
          <tr><th>Инфраструктура</th><td className="num">{fmtEur(c.today_eur.infra)}</td><td className="num">{fmtEur(c.total_eur.infra)}</td></tr>
          <tr><th>Такси (симулирани)</th><td className="num">—</td><td className="num">{fmtEur(c.trading_fees_eur)}</td></tr>
        </tbody>
      </table>
      <p><small>Jev заявки: {c.jev_calls.map((j) => `${j.status} ${j.count}`).join(", ") || "няма"}</small></p>
      <ul style={{ paddingLeft: 18, margin: 0 }}>{c.notes.map((n) => <li key={n}><small>{n}</small></li>)}</ul>
    </section>
  );
}

export function LatencyPanel({ l }: { l: Latency }) {
  const names: Record<string, string> = {
    quote_receive: "получаване на котировка",
    analysis: "анализ (общо)",
    jev_call: "заявка към Jev",
    submit_to_ack: "подаване → потвърждение",
    submit_to_fill: "подаване → изпълнение",
  };
  return (
    <section className="card span-4" aria-labelledby="lat-h">
      <h2 id="lat-h">Закъснения ({l.hours} ч)</h2>
      {l.stages.length === 0 ? <p className="muted">Няма измервания.</p> : (
        <table>
          <thead><tr><th>етап</th><th className="num">n</th><th className="num">p50</th><th className="num">p95</th></tr></thead>
          <tbody>
            {l.stages.map((s) => (
              <tr key={s.stage}>
                <td>{names[s.stage] ?? s.stage}</td><td className="num">{s.n}</td>
                <td className="num">{Math.round(Number(s.p50_ms))} ms</td><td className="num">{Math.round(Number(s.p95_ms))} ms</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      <p><small>{l.note}</small></p>
    </section>
  );
}

function CalRow({ name, e }: { name: string; e: CalibrationEntry | null }) {
  if (!e) return <tr><td>{name}</td><td colSpan={4} className="muted">няма данни</td></tr>;
  return (
    <tr>
      <td>{name}</td><td className="num">{e.n}</td><td className="num">{fmtProb(e.mean_forecast)}</td>
      <td className="num">{fmtProb(e.observed_rate)}</td>
      <td className="num">{e.brier.toFixed(3)} / {e.brier_of_base_rate.toFixed(3)}</td>
    </tr>
  );
}

const CAL_EVENTS = [
  { key: "close_above", name: "над целта след 30 мин" },
  { key: "touch", name: "докосва целта" },
  { key: "close_below", name: "под стопа след 30 мин" },
] as const;

export function CalibrationPanel({ c }: { c: Calibration }) {
  const [event, setEvent] = useState<(typeof CAL_EVENTS)[number]["key"]>("close_above");
  const current = CAL_EVENTS.find((e) => e.key === event)!;
  return (
    <section className="card span-6" aria-labelledby="cal-h">
      <h2 id="cal-h">Калибрация на прогнозите</h2>
      <div className="segmented" role="group" aria-label="Събитие">
        {CAL_EVENTS.map((e) => (
          <button key={e.key} type="button" aria-pressed={event === e.key} onClick={() => setEvent(e.key)}>{e.name}</button>
        ))}
      </div>
      <ReliabilityChart key={event} entry={c[event]} label={current.name} />
      <div className="table-wrap">
        <table>
          <thead><tr><th>събитие</th><th className="num">n</th><th className="num">прогноза</th><th className="num">стана</th><th className="num">Brier / база</th></tr></thead>
          <tbody>
            {CAL_EVENTS.map((e) => <CalRow key={e.key} name={e.name} e={c[e.key]} />)}
          </tbody>
        </table>
      </div>
      <p className="notice">{c.note}</p>
      <p><small>{c.explanation}</small></p>
    </section>
  );
}

export function AssumptionsPanel({ p, settings }: { p: Portfolio; settings: SettingsView | null }) {
  const a = p.simulation_assumptions;
  const limits: [string, string][] = settings ? [
    ["Макс. стойност на поръчка", fmtEur(String(settings.risk_limits.max_order_eur))],
    ["Макс. експозиция", fmtEur(String(settings.risk_limits.max_exposure_eur))],
    ["Дневна загуба (вкл. нереализирана)", fmtEur(String(settings.risk_limits.daily_loss_limit_eur))],
    ["Сделки на ден", String(settings.risk_limits.max_trades_per_day)],
    ["Пауза между сделки", `${settings.risk_limits.min_seconds_between_trades} с`],
    ["Макс. спред", fmtPct(String(settings.risk_limits.max_spread_pct))],
    ["Макс. възраст на котировка", `${settings.risk_limits.max_quote_age_s} с`],
  ] : [];
  return (
    <section className="card span-6" aria-labelledby="assume-h">
      <h2 id="assume-h">Допускания на симулацията и лимити</h2>
      <table>
        <tbody>
          <tr><th>Такса (taker)</th><td className="num">{fmtPct(Number(a.fee_rate) * 100)}</td></tr>
          <tr><th>Приплъзване</th><td className="num">{a.slippage_bps} bps</td></tr>
          <tr><th>Мин. количество / стъпка</th><td className="num">{a.min_qty} / {a.qty_step} BTC</td></tr>
          <tr><th>Мин. стойност</th><td className="num">{fmtEur(a.min_cost)}</td></tr>
          <tr><th>Частични изпълнения</th><td className="num">{a.partial_fills ? `да, до ${fmtPct(Number(a.top_of_book_participation) * 100, 0)} от видимото количество` : "не"}</td></tr>
          {limits.map(([k, v]) => <tr key={k}><th>{k}</th><td className="num">{v}</td></tr>)}
        </tbody>
      </table>
      <ul style={{ paddingLeft: 18 }}>{a.notes.map((n) => <li key={n}><small>{n}</small></li>)}</ul>
      {settings && <p><small>{settings.note} Версия на конфигурацията: <span className="mono">{settings.config_version}</span></small></p>}
    </section>
  );
}
