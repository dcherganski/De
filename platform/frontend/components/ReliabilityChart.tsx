"use client";

import { useRef, useState } from "react";
import type { CalibrationBin, CalibrationEntry } from "@/lib/types";
import { fmtProb } from "@/lib/format";

// Reliability diagram: forecast probability (x) against how often the event really happened (y), one axis
// in probability units. The diagonal is perfect calibration. Whiskers are 95% Wilson intervals, so small
// bins look as uncertain as they are. A separate strip below shows how many forecasts fell in each bin.
const W = 400;
const H = 220;
const MIN_TOTAL = 10;
const FEW = 5;
const TICKS = [0, 0.2, 0.4, 0.6, 0.8, 1];

const pct = (v: number) => `${(v * 100).toFixed(2)}%`;
const top = (v: number) => `${((1 - v) * 100).toFixed(2)}%`;
const range = (b: CalibrationBin) => `${Math.round(b.lo * 100)}–${Math.round(b.hi * 100)}%`;

export function ReliabilityChart({ entry, label }: { entry: CalibrationEntry | null; label: string }) {
  const [active, setActive] = useState<number | null>(null);
  const plotRef = useRef<HTMLDivElement>(null);

  if (!entry || entry.n < MIN_TOTAL) {
    return (
      <p className="muted">
        Още няма достатъчно прогнози с резултат за „{label}“ (n={entry?.n ?? 0}). Графиката се показва от {MIN_TOTAL} нагоре;
        под ~100 тя е предимно шум.
      </p>
    );
  }
  const bins = entry.bins;
  const last = bins.length - 1;
  const maxN = Math.max(...bins.map((b) => b.n));

  function pick(clientX: number) {
    const box = plotRef.current?.getBoundingClientRect();
    if (!box || bins.length === 0) return;
    const x = (clientX - box.left) / box.width;
    let best = 0;
    bins.forEach((b, i) => { if (Math.abs(b.mean_forecast - x) < Math.abs(bins[best].mean_forecast - x)) best = i; });
    setActive(best);
  }

  function onKey(e: React.KeyboardEvent) {
    if (e.key === "ArrowLeft") setActive((i) => Math.max(0, (i ?? 0) - 1));
    else if (e.key === "ArrowRight") setActive((i) => Math.min(last, (i ?? -1) + 1));
    else return;
    e.preventDefault();
  }

  const a = active !== null ? bins[active] : null;
  return (
    <figure className="chart-figure">
      <div className="legend" aria-hidden="true">
        <span><i className="key key-1" />{label}: колко често се случва</span>
        <span><i className="key key-diag" />идеална калибрация</span>
      </div>
      <div className="chart-area">
        <div className="y-labels" aria-hidden="true">
          {TICKS.map((t) => <span key={t} style={{ top: top(t) }}>{fmtProb(t)}</span>)}
        </div>
        <div
          ref={plotRef}
          className="plot plot-rel"
          tabIndex={0}
          role="img"
          aria-label={`Калибрация за „${label}“: ${entry.n} прогнози в ${bins.length} интервала. Средна прогноза ${fmtProb(entry.mean_forecast)}, случило се ${fmtProb(entry.observed_rate)}. Стрелки наляво и надясно минават през интервалите.`}
          onPointerMove={(e) => pick(e.clientX)}
          onPointerLeave={() => setActive(null)}
          onFocus={() => setActive(0)}
          onBlur={() => setActive(null)}
          onKeyDown={onKey}
        >
          <svg className="chart" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">
            {TICKS.map((t) => (
              <line key={t} x1="0" x2={W} y1={(1 - t) * H} y2={(1 - t) * H} className="grid" vectorEffect="non-scaling-stroke" />
            ))}
            <line x1="0" y1={H} x2={W} y2="0" className="diag" vectorEffect="non-scaling-stroke" />
            {bins.map((b) => (
              <line key={b.lo} x1={b.mean_forecast * W} x2={b.mean_forecast * W} y1={(1 - b.ci_lo) * H} y2={(1 - b.ci_hi) * H}
                className="whisker" vectorEffect="non-scaling-stroke" />
            ))}
          </svg>
          {a && <div className="crosshair" style={{ left: pct(a.mean_forecast) }} />}
          {bins.map((b, i) => (
            <span key={b.lo} className={`dot dot-1 ${b.n < FEW ? "hollow" : ""} ${i === active ? "on" : ""}`}
              style={{ left: pct(b.mean_forecast), top: top(b.observed_rate) }} />
          ))}
          {a && (
            <div className={`tooltip pinned ${a.mean_forecast > 0.5 ? "at-left" : "at-right"}`}>
              <div className="muted">прогноза {range(a)} · средно {fmtProb(a.mean_forecast)}</div>
              <div><i className="key key-1" /><strong>{fmtProb(a.observed_rate)}</strong> <span className="muted">се случи ({a.hits} от {a.n})</span></div>
              <div className="muted">95%: {fmtProb(a.ci_lo)}–{fmtProb(a.ci_hi)}{a.n < FEW ? " · малко данни" : ""}</div>
            </div>
          )}
        </div>
      </div>
      <div className="chart-area">
        <div className="y-labels" aria-hidden="true"><span style={{ top: "50%" }}>брой</span></div>
        <div className="count-strip" aria-hidden="true">
          {bins.map((b, i) => (
            <span key={b.lo} className={`bar ${i === active ? "on" : ""}`}
              style={{ left: `calc(${pct(b.lo)} + 1px)`, width: "calc(10% - 2px)", height: `${(b.n / maxN) * 100}%` }} />
          ))}
        </div>
      </div>
      <div className="x-labels x-ticks" aria-hidden="true">
        {TICKS.map((t) => <small key={t} style={{ left: pct(t) }}>{fmtProb(t)}</small>)}
      </div>
      <p className="axis-title"><small>Прогноза на Jev → (точка над диагонала: случва се по-често от прогнозата; под нея: по-рядко)</small></p>
      <details>
        <summary><small>Таблица по интервали</small></summary>
        <div className="table-wrap">
          <table>
            <thead><tr><th>прогноза</th><th className="num">n</th><th className="num">средна прогноза</th><th className="num">случи се</th><th className="num">95% интервал</th></tr></thead>
            <tbody>
              {bins.map((b) => (
                <tr key={b.lo}>
                  <td>{range(b)}</td>
                  <td className="num">{b.n}</td>
                  <td className="num">{fmtProb(b.mean_forecast)}</td>
                  <td className="num">{fmtProb(b.observed_rate)} ({b.hits})</td>
                  <td className="num">{fmtProb(b.ci_lo)}–{fmtProb(b.ci_hi)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}
