"use client";

import { useRef, useState } from "react";
import { api, ApiError } from "@/lib/api";
import type { Credential, Status, Venues } from "@/lib/types";
import { fmtTime } from "@/lib/format";

function useAction(after: () => void) {
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<{ text: string; error: boolean; details?: string[] } | null>(null);
  async function run<T>(fn: () => Promise<T>, ok: (r: T) => string) {
    setBusy(true);
    setMessage(null);
    try {
      const r = await fn();
      setMessage({ text: ok(r), error: false });
      after();
    } catch (e) {
      setMessage(e instanceof ApiError ? { text: e.message, error: true, details: e.details } : { text: "Грешка.", error: true });
    } finally {
      setBusy(false);
    }
  }
  return { busy, message, run };
}

function Message({ m }: { m: { text: string; error: boolean; details?: string[] } | null }) {
  if (!m) return null;
  return (
    <div className={`notice ${m.error ? "error" : ""}`} role="status" aria-live="polite">
      {m.text}
      {m.details && <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>{m.details.map((d) => <li key={d}>{d}</li>)}</ul>}
    </div>
  );
}

export function Controls({ status, refresh }: { status: Status; refresh: () => void }) {
  const { busy, message, run } = useAction(refresh);
  const dialog = useRef<HTMLDialogElement>(null);
  const [understood, setUnderstood] = useState(false);
  const paused = status.bot?.entries_paused ?? false;
  const pendingSell = status.pending_commands.some((c) => c.kind === "sell_position");

  return (
    <section className="card span-6" aria-labelledby="ctl-h">
      <h2 id="ctl-h">Управление</h2>
      <div className="row">
        {paused ? (
          <button className="primary" disabled={busy} onClick={() => run(() => api.post("/api/control/resume"), () => "Новите поръчки са разрешени отново.")}>
            Възобнови новите поръчки
          </button>
        ) : (
          <button disabled={busy} onClick={() => run(() => api.post<{ note: string }>("/api/control/pause", { reason: "спряно от таблото" }), (r) => r.note)}>
            Спри новите поръчки
          </button>
        )}
        <button disabled={busy || status.open_orders === 0}
          onClick={() => run(() => api.post<{ duplicate: boolean }>("/api/control/cancel-pending"),
            (r) => (r.duplicate ? "Вече има заявка за отмяна." : "Заявката за отмяна е приета; двигателят ще я изпълни."))}>
          Отмени чакащите поръчки ({status.open_orders})
        </button>
        <button className="danger" disabled={busy || pendingSell} onClick={() => { setUnderstood(false); dialog.current?.showModal(); }}>
          Продай позицията
        </button>
      </div>
      <p><small>
        Спирането на новите поръчки <strong>не продава</strong> нищо. {paused && status.bot?.paused_reason ? `Причина: ${status.bot.paused_reason}.` : ""}
        {pendingSell && " Има чакаща заявка за продажба."}
      </small></p>
      <Message m={message} />
      <dialog ref={dialog} aria-labelledby="sell-h">
        <h2 id="sell-h">Продай позицията</h2>
        <p>Пазарна продажба на <strong>целия свободен BTC</strong>, включително началния, по текущия bid.</p>
        <ul>
          <li>Цената не е гарантирана; таксата и приплъзването намаляват сумата.</li>
          <li>Двигателят пак проверява свежестта на котировката и спреда и може да откаже.</li>
          <li>В режим {status.mode} {status.mode === "PAPER" ? "продажбата е симулирана." : "продажбата е реална."}</li>
        </ul>
        <label className="check">
          <input type="checkbox" checked={understood} onChange={(e) => setUnderstood(e.target.checked)} />
          Разбирам и искам да продам.
        </label>
        <div className="row" style={{ marginTop: 12 }}>
          <button className="danger" disabled={!understood || busy} onClick={async () => {
            dialog.current?.close();
            await run(() => api.post<{ note: string; duplicate: boolean }>("/api/control/sell-position", { confirm: true }),
              (r) => (r.duplicate ? "Вече има заявка за продажба." : r.note));
          }}>Продай</button>
          <button onClick={() => dialog.current?.close()}>Отказ</button>
        </div>
      </dialog>
    </section>
  );
}

export function VenuePicker({ venues, refresh }: { venues: Venues; refresh: () => void }) {
  const [venue, setVenue] = useState(venues.selected_venue);
  const [symbol, setSymbol] = useState(venues.selected_symbol);
  const { busy, message, run } = useAction(refresh);
  const selectable = venues.venues.filter((v) => v.available && v.market_data);
  const changed = venue !== venues.selected_venue || symbol !== venues.selected_symbol;
  return (
    <section className="card span-6" aria-labelledby="venue-h">
      <h2 id="venue-h">Борса и двойка</h2>
      <div className="row" style={{ alignItems: "flex-end" }}>
        <label>
          Борса
          <select value={venue} onChange={(e) => setVenue(e.target.value)}>
            {venues.venues.filter((v) => v.name !== "paper").map((v) => (
              <option key={v.name} value={v.name} disabled={!selectable.includes(v)}>
                {v.name}{!v.available ? " (недостъпна)" : ""}
              </option>
            ))}
          </select>
        </label>
        <label>
          Двойка
          <select value={symbol} onChange={(e) => setSymbol(e.target.value)}>
            {venues.symbols.map((s) => <option key={s.symbol} value={s.symbol} disabled={!s.supported}>{s.symbol}</option>)}
          </select>
        </label>
        <button className="primary" disabled={busy || !changed}
          onClick={() => run(() => api.post<{ note: string }>("/api/control/venue", { venue, symbol }), (r) => r.note)}>
          Приложи
        </button>
      </div>
      <p><small>{venues.note} Активен поток: {venues.active_feed ?? "—"}.</small></p>
      <Message m={message} />
      <details>
        <summary><small>Какво може всяка борса в тази версия</small></summary>
        <ul className="stack" style={{ paddingLeft: 18 }}>
          {venues.venues.map((v) => (
            <li key={v.name}>
              <strong>{v.name}</strong>: {v.reason}
              {v.docs.length > 0 && <div><small>Проверено {v.checked_on}: {v.docs.map((d) => (
                <a key={d} href={d} target="_blank" rel="noopener noreferrer" style={{ marginRight: 8 }}>{new URL(d).hostname}</a>
              ))}</small></div>}
            </li>
          ))}
        </ul>
      </details>
    </section>
  );
}

export function LivePanel({ status }: { status: Status }) {
  return (
    <section className="card span-6" aria-labelledby="live-h">
      <h2 id="live-h">Реална търговия (LIVE)</h2>
      <p>LIVE е <strong>изключен</strong>. Таблото не може да го включи.</p>
      <ul style={{ paddingLeft: 18 }}>{status.live.blocked_because.map((r) => <li key={r}>{r}</li>)}</ul>
    </section>
  );
}

interface CheckResult {
  ok: boolean;
  problems: string[];
  read_only?: boolean;
  can_trade_spot?: boolean;
  ip_restricted?: boolean;
  key_type?: string;
  region?: string;
}

export function CredentialsPanel({ data, refresh }: {
  data: { credentials: Credential[]; storage_enabled: boolean; note: string } | null;
  refresh: () => void;
}) {
  const { busy, message, run } = useAction(refresh);
  const [form, setForm] = useState({ venue: "bybit", label: "main", api_key: "", api_secret: "", permissions_note: "", ok: false });
  const [checks, setChecks] = useState<Record<number, CheckResult>>({});
  if (!data) return null;
  return (
    <section className="card span-6" aria-labelledby="cred-h">
      <h2 id="cred-h">API ключове на борсата</h2>
      <p><small>{data.note} Създай ключ само с права за spot търговия, без теглене и прехвърляне, по възможност с IP ограничение.</small></p>
      {data.credentials.length > 0 && (
        <ul className="stack" style={{ paddingLeft: 18 }}>
          {data.credentials.map((c) => (
            <li key={c.id}>
              <strong>{c.venue}</strong> · {c.label} · <span className="mono">{c.key_hint}</span> · {fmtTime(c.created_at)}
              <div className="row" style={{ marginTop: 4 }}>
                {c.venue === "bybit" && (
                  <button disabled={busy} onClick={() => run(async () => {
                    const r = await api.post<CheckResult>(`/api/credentials/${c.id}/check`);
                    setChecks((x) => ({ ...x, [c.id]: r }));
                    return r;
                  }, (r) => (r.ok ? "Ключът е проверен: няма права за теглене или прехвърляне." : "Ключът има проблем, виж по-долу."))}>
                    Провери правата
                  </button>
                )}
                <button disabled={busy} onClick={() => run(() => api.del(`/api/credentials/${c.id}`), () => "Ключът е изтрит.")}>Изтрий</button>
              </div>
              {checks[c.id] && (
                <div className={`notice ${checks[c.id].ok ? "" : "error"}`}>
                  {checks[c.id].ok ? "Без права за теглене/прехвърляне." : "Проблеми:"}
                  {checks[c.id].problems.length > 0 && <ul style={{ margin: 0, paddingLeft: 18 }}>{checks[c.id].problems.map((p) => <li key={p}>{p}</li>)}</ul>}
                  {checks[c.id].can_trade_spot !== undefined && (
                    <div><small>
                      spot търговия: {checks[c.id].can_trade_spot ? "да" : "не"} · само четене: {checks[c.id].read_only ? "да" : "не"} ·
                      IP ограничение: {checks[c.id].ip_restricted ? "да" : "не"} · регион: {checks[c.id].region}
                    </small></div>
                  )}
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
      {!data.storage_enabled ? (
        <p className="notice">Съхранението е изключено: на сървъра липсва SECRETS_MASTER_KEY (виж README).</p>
      ) : (
        <form className="stack" autoComplete="off" onSubmit={(e) => {
          e.preventDefault();
          run(() => api.post("/api/credentials", {
            venue: form.venue, label: form.label, api_key: form.api_key, api_secret: form.api_secret,
            permissions_note: form.permissions_note, confirm_no_withdrawal: form.ok,
          }), () => "Ключът е запазен криптиран.").then(() => setForm((f) => ({ ...f, api_key: "", api_secret: "", ok: false })));
        }}>
          <div className="row">
            <label>Борса
              <select value={form.venue} onChange={(e) => setForm({ ...form, venue: e.target.value })}>
                <option value="bybit">bybit</option>
                <option value="kraken">kraken</option>
              </select>
            </label>
            <label>Етикет<input value={form.label} maxLength={64} onChange={(e) => setForm({ ...form, label: e.target.value })} /></label>
          </div>
          <label>API ключ<input type="password" autoComplete="off" value={form.api_key} onChange={(e) => setForm({ ...form, api_key: e.target.value })} required /></label>
          <label>API тайна<input type="password" autoComplete="off" value={form.api_secret} onChange={(e) => setForm({ ...form, api_secret: e.target.value })} required /></label>
          <label>Бележка за правата<input value={form.permissions_note} maxLength={500} onChange={(e) => setForm({ ...form, permissions_note: e.target.value })} /></label>
          <label className="check">
            <input type="checkbox" checked={form.ok} onChange={(e) => setForm({ ...form, ok: e.target.checked })} required />
            Ключът няма право за теглене или прехвърляне на средства.
          </label>
          <button className="primary" type="submit" disabled={busy || !form.ok}>Запази криптиран</button>
        </form>
      )}
      <Message m={message} />
    </section>
  );
}
