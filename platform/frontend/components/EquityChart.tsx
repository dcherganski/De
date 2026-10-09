"use client";

import { useMemo, useRef, useState } from "react";
import type { EquityPoint } from "@/lib/types";
import { fmtEur, fmtTime } from "@/lib/format";

// Two series, categorical slots 1 and 2 (validated for colour-blind separation in light and dark mode
// against this page's card surfaces). Identity is also carried by the legend and the table view.
const W = 600;
const H = 200;
const PAD_Y = 8;

function niceTicks(lo: number, hi: number, count = 4): number[] {
  const span = hi - lo || 1;
  const raw = span / count;
  const mag = 10 ** Math.floor(Math.log10(raw));
  const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((s) => s >= raw) ?? raw;
  const start = Math.ceil(lo / step) * step;
  const ticks = [];
  for (let v = start; v <= hi + 1e-9; v += step) ticks.push(Number(v.toFixed(6)));
  return ticks;
}

/** Bot equity vs. holding the starting BTC, both marked at the bid. One axis, EUR. */
export function EquityChart({ points }: { points: EquityPoint[] }) {
  const [active, setActive] = useState<number | null>(null);
  const plotRef = useRef<HTMLDivElement>(null);

  const model = useMemo(() => {
    const eq = points.map((p) => Number(p.equity_eur));
    const bench = points.map((p) => Number(p.benchmark_eur));
    let lo = Math.min(...eq, ...bench);
    let hi = Math.max(...eq, ...bench);
    if (hi - lo < 1) {
      lo -= 0.5;
      hi += 0.5;
    }
    const pad = (hi - lo) * 0.08;
    lo -= pad;
    hi += pad;
    const xPct = (i: number) => (points.length < 2 ? 50 : (i / (points.length - 1)) * 100);
    const yPct = (v: number) => (PAD_Y + ((hi - v) / (hi - lo)) * (H - 2 * PAD_Y)) / H * 100;
    const path = (vals: number[]) =>
      vals.map((v, i) => `${i ? "L" : "M"}${(xPct(i) * W / 100).toFixed(2)},${(yPct(v) * H / 100).toFixed(2)}`).join(" ");
    return { eq, bench, lo, hi, xPct, yPct, eqPath: path(eq), benchPath: path(bench), ticks: niceTicks(lo, hi) };
  }, [points]);

  if (points.length < 2) return <p className="muted">Още няма достатъчно точки за графика.</p>;
  const last = points.length - 1;
  const shown = active ?? last;

  function pick(clientX: number) {
    const box = plotRef.current?.getBoundingClientRect();
    if (!box) return;
    const ratio = Math.min(1, Math.max(0, (clientX - box.left) / box.width));
    setActive(Math.round(ratio * last));
  }

  function onKey(e: React.KeyboardEvent) {
    if (e.key === "ArrowLeft") setActive((i) => Math.max(0, (i ?? last) - 1));
    else if (e.key === "ArrowRight") setActive((i) => Math.min(last, (i ?? last) + 1));
    else return;
    e.preventDefault();
  }

  return (
    <figure className="chart-figure">
      <div className="legend" aria-hidden="true">
        <span><i className="key key-1" />Ботът · {fmtEur(model.eq[shown])}</span>
        <span><i className="key key-2" />Задържане на началния BTC · {fmtEur(model.bench[shown])}</span>
      </div>
      <div className="chart-area">
        <div className="y-labels" aria-hidden="true">
          {model.ticks.map((t) => (
            <span key={t} style={{ top: `${model.yPct(t)}%` }}>{fmtEur(t)}</span>
          ))}
        </div>
        <div
          ref={plotRef}
          className="plot"
          tabIndex={0}
          role="img"
          aria-label={`Стойност на портфейла спрямо задържане на началния BTC. Последно: ботът ${fmtEur(model.eq[last])}, задържане ${fmtEur(model.bench[last])}. Стрелки наляво и надясно показват отделните точки.`}
          onPointerMove={(e) => pick(e.clientX)}
          onPointerLeave={() => setActive(null)}
          onFocus={() => setActive(last)}
          onBlur={() => setActive(null)}
          onKeyDown={onKey}
        >
          <svg className="chart" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" aria-hidden="true">
            {model.ticks.map((t) => {
              const y = (model.yPct(t) * H) / 100;
              return <line key={t} x1="0" x2={W} y1={y} y2={y} className="grid" vectorEffect="non-scaling-stroke" />;
            })}
            <path d={model.benchPath} className="line line-2" vectorEffect="non-scaling-stroke" />
            <path d={model.eqPath} className="line line-1" vectorEffect="non-scaling-stroke" />
          </svg>
          {active !== null && <div className="crosshair" style={{ left: `${model.xPct(active)}%` }} />}
          {[["dot-2", model.bench], ["dot-1", model.eq]].map(([cls, vals]) => (
            <span key={cls as string} className={`dot ${cls}`}
              style={{ left: `${model.xPct(shown)}%`, top: `${model.yPct((vals as number[])[shown])}%` }} />
          ))}
          {active !== null && (
            <div className={`tooltip ${model.xPct(active) > 60 ? "left" : ""}`} style={{ left: `${model.xPct(active)}%` }}>
              <div className="muted">{fmtTime(points[active].ts)}</div>
              <div><i className="key key-1" /><strong>{fmtEur(model.eq[active])}</strong> <span className="muted">ботът</span></div>
              <div><i className="key key-2" /><strong>{fmtEur(model.bench[active])}</strong> <span className="muted">задържане</span></div>
            </div>
          )}
        </div>
      </div>
      <div className="row x-labels">
        <small>{fmtTime(points[0].ts)}</small>
        <small>{fmtTime(points[last].ts)}</small>
      </div>
      <details>
        <summary><small>Таблица със стойностите</small></summary>
        <div className="table-wrap" style={{ maxHeight: 240 }}>
          <table>
            <thead><tr><th>време</th><th className="num">ботът</th><th className="num">задържане</th><th className="num">спад</th></tr></thead>
            <tbody>
              {points.slice().reverse().map((p) => (
                <tr key={p.ts}>
                  <td>{fmtTime(p.ts)}</td>
                  <td className="num">{fmtEur(p.equity_eur)}</td>
                  <td className="num">{fmtEur(p.benchmark_eur)}</td>
                  <td className="num">{Number(p.drawdown_pct).toFixed(2)}%</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </details>
    </figure>
  );
}
