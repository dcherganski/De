"use client";

import { useRouter } from "next/navigation";
import { useCallback } from "react";
import { EquityChart } from "@/components/EquityChart";
import { Controls, CredentialsPanel, LivePanel, VenuePicker } from "@/components/controls";
import {
  AssumptionsPanel, CalibrationPanel, CostsPanel, LatencyPanel, PortfolioPanel, SignalPanel, StatusPanel,
} from "@/components/panels";
import { NewsList, OrdersTable, SignalsTable } from "@/components/tables";
import { api } from "@/lib/api";
import type {
  Calibration, Costs, Credential, EquityPoint, Latency, NewsView, OrderView, Portfolio, SettingsView, SignalView,
  Status, Venues,
} from "@/lib/types";
import { usePoll } from "@/lib/usePoll";

export default function Dashboard() {
  const router = useRouter();
  const toLogin = useCallback(() => router.replace("/login"), [router]);

  const status = usePoll<Status>("/api/status", 5000, toLogin);
  const portfolio = usePoll<Portfolio>("/api/portfolio", 5000, toLogin);
  const equity = usePoll<EquityPoint[]>("/api/equity?hours=24&points=300", 30000, toLogin);
  const signals = usePoll<SignalView[]>("/api/signals?limit=30", 15000, toLogin);
  const orders = usePoll<OrderView[]>("/api/orders?limit=30", 10000, toLogin);
  const news = usePoll<NewsView[]>("/api/news?limit=30", 60000, toLogin);
  const costs = usePoll<Costs>("/api/costs", 60000, toLogin);
  const latency = usePoll<Latency>("/api/latency", 60000, toLogin);
  const calibration = usePoll<Calibration>("/api/calibration", 120000, toLogin);
  const venues = usePoll<Venues>("/api/venues", 60000, toLogin);
  const settings = usePoll<SettingsView>("/api/settings", 300000, toLogin);
  const creds = usePoll<{ credentials: Credential[]; storage_enabled: boolean; note: string }>("/api/credentials", 300000, toLogin);

  const refreshAll = () => {
    status.refresh();
    portfolio.refresh();
    orders.refresh();
    venues.refresh();
    creds.refresh();
  };

  const s = status.data;
  const fixture = Boolean(s?.quote?.is_fixture || s?.engine?.is_fixture || portfolio.data?.init?.is_fixture);
  const live = s?.mode === "LIVE";

  return (
    <main className="wrap">
      <header className="topbar">
        <div>
          <h1>Крипто табло</h1>
          <small>{s?.bot ? `${s.bot.selected_venue} · ${s.bot.selected_symbol}` : "зареждане…"}</small>
        </div>
        <div className="row">
          {s && (
            <span className={`mode ${live ? "live" : "paper"}`} aria-label={`Режим ${s.mode}`}>
              {live ? "LIVE · РЕАЛНИ ПАРИ" : "PAPER · СИМУЛАЦИЯ"}
            </span>
          )}
          <button onClick={async () => { await api.post("/api/auth/logout").catch(() => null); toLogin(); }}>Изход</button>
        </div>
      </header>

      {fixture && <div className="banner warn" role="alert">ТЕСТОВИ ДАННИ: цените и портфейлът са от фикстура, не са живи пазарни данни.</div>}
      {status.error && <div className="banner bad" role="alert">Няма връзка с API: {status.error}</div>}
      {s?.engine && !s.engine.running && <div className="banner warn" role="alert">Двигателят не отговаря — нови решения не се вземат.</div>}

      {!s ? <p className="muted">Зареждане…</p> : (
        <div className="grid">
          <StatusPanel status={s} />
          {portfolio.data && <PortfolioPanel p={portfolio.data} />}
          <section className="card span-12" aria-labelledby="eq-h">
            <h2 id="eq-h">Стойност за 24 часа</h2>
            <EquityChart points={equity.data ?? []} />
          </section>
          <SignalPanel status={s} />
          <Controls status={s} refresh={refreshAll} />
          {venues.data && <VenuePicker key={`${venues.data.selected_venue}-${venues.data.selected_symbol}`} venues={venues.data} refresh={refreshAll} />}
          <LivePanel status={s} />
          {orders.data && <OrdersTable orders={orders.data} />}
          {signals.data && <SignalsTable signals={signals.data} />}
          {costs.data && <CostsPanel c={costs.data} />}
          {latency.data && <LatencyPanel l={latency.data} />}
          {calibration.data && <CalibrationPanel c={calibration.data} />}
          {news.data && <NewsList news={news.data} />}
          <CredentialsPanel data={creds.data} refresh={refreshAll} />
          {portfolio.data && <AssumptionsPanel p={portfolio.data} settings={settings.data} />}
        </div>
      )}
      <footer style={{ marginTop: 24 }}>
        <small>
          Jev оценява вероятности; това не е обещание за печалба. Праговете са експериментални. Котировките са от борсата за
          данни и не са цена на изпълнение в Robinhood.
        </small>
      </footer>
    </main>
  );
}
