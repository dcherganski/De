import type { NewsView, OrderView, SignalView } from "@/lib/types";
import { ACTION_LABEL, fmtAge, fmtBtc, fmtEur, fmtPct, fmtPrice, fmtProb, fmtTime, INTENT_LABEL, STATUS_LABEL } from "@/lib/format";

export function OrdersTable({ orders }: { orders: OrderView[] }) {
  return (
    <section className="card span-12" aria-labelledby="orders-h">
      <h2 id="orders-h">Поръчки и история</h2>
      {orders.length === 0 ? <p className="muted">Още няма поръчки.</p> : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>време</th><th>вид</th><th>страна</th><th className="num">количество</th><th className="num">изпълнено</th>
                <th className="num">средна цена</th><th className="num">такса</th><th>статус</th><th>режим</th>
              </tr>
            </thead>
            <tbody>
              {orders.map((o) => (
                <tr key={o.id}>
                  <td>{fmtTime(o.created_at)}</td>
                  <td>{INTENT_LABEL[o.intent] ?? o.intent}</td>
                  <td>{o.side === "buy" ? "покупка" : "продажба"}</td>
                  <td className="num">{fmtBtc(o.qty)}</td>
                  <td className="num">{fmtBtc(o.filled_qty)}</td>
                  <td className="num">{fmtPrice(o.avg_price)}</td>
                  <td className="num">{fmtEur(o.fee_quote)}</td>
                  <td>
                    {STATUS_LABEL[o.status] ?? o.status}
                    {o.error && <div><small>{o.error}</small></div>}
                  </td>
                  <td><span className="pill">{o.mode.toUpperCase()} · {o.venue}</span></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

function outcomeText(s: SignalView): string {
  if (!s.outcome) return "чака 30 мин";
  const o = s.outcome;
  if (o.mid === null) return o.note ?? "няма данни";
  return `${o.close_above ? "над целта" : "под целта"}${o.touch ? ", докосна" : ""}`;
}

export function SignalsTable({ signals }: { signals: SignalView[] }) {
  return (
    <section className="card span-12" aria-labelledby="signals-h">
      <h2 id="signals-h">Сигнали и резултат след 30 минути</h2>
      {signals.length === 0 ? <p className="muted">Още няма сигнали.</p> : (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>време</th><th>повод</th><th>действие</th><th className="num">цел</th><th className="num">P(над целта)</th>
                <th className="num">P(докосва)</th><th className="num">P(под стопа)</th><th>резултат</th><th>бележка</th>
              </tr>
            </thead>
            <tbody>
              {signals.map((s) => (
                <tr key={s.id}>
                  <td title={fmtTime(s.created_at)}>{fmtAge(s.age_s)}</td>
                  <td>{s.trigger === "news" ? "новина" : "свещ"}</td>
                  <td>{ACTION_LABEL[s.action] ?? s.action}{!s.valid && " (невалиден)"}</td>
                  <td className="num">+{fmtPct(s.target_pct)}</td>
                  <td className="num">{fmtProb(s.p_close_above)}</td>
                  <td className="num">{fmtProb(s.p_touch)}</td>
                  <td className="num">{fmtProb(s.p_close_below)}</td>
                  <td>{outcomeText(s)}</td>
                  <td><small>{s.reason}</small></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}

export function NewsList({ news }: { news: NewsView[] }) {
  return (
    <section className="card span-6" aria-labelledby="news-h">
      <h2 id="news-h">Новини</h2>
      <p><small>Новините се подават на Jev като недоверени данни, не като инструкции.</small></p>
      {news.length === 0 ? <p className="muted">Няма новини.</p> : (
        <ul className="stack" style={{ paddingLeft: 18, margin: 0 }}>
          {news.slice(0, 15).map((n) => (
            <li key={n.id}>
              <a href={n.url} target="_blank" rel="noopener noreferrer nofollow">{n.title}</a>
              <div>
                <small>
                  {n.source} · публикувана {fmtTime(n.published_at)} · закъснение {n.latency_s ? `${Math.round(Number(n.latency_s))} с` : "—"}
                  {n.relevant && " · свързана с BTC"}
                  {n.triggered_signal_id && " · предизвика преоценка"}
                  {n.is_fixture && " · ТЕСТОВИ ДАННИ"}
                </small>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
