import type { EquityPoint } from "@/lib/types";
import { fmtEur, fmtTime } from "@/lib/format";

const W = 600;
const H = 200;
const PAD = 6;

/** Bot equity vs. holding the starting BTC, both marked at the bid. */
export function EquityChart({ points }: { points: EquityPoint[] }) {
  if (points.length < 2) return <p className="muted">Още няма достатъчно точки за графика.</p>;
  const eq = points.map((p) => Number(p.equity_eur));
  const bench = points.map((p) => Number(p.benchmark_eur));
  const all = eq.concat(bench);
  let lo = Math.min(...all);
  let hi = Math.max(...all);
  if (hi - lo < 0.01) {
    hi += 0.5;
    lo -= 0.5;
  }
  const x = (i: number) => PAD + (i / (points.length - 1)) * (W - 2 * PAD);
  const y = (v: number) => H - PAD - ((v - lo) / (hi - lo)) * (H - 2 * PAD);
  const path = (vals: number[]) => vals.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  return (
    <figure style={{ margin: 0 }}>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <div className="legend">
          <span><i style={{ background: "var(--line-equity)" }} />Ботът</span>
          <span><i style={{ background: "var(--line-bench)" }} />Задържане на началния BTC</span>
        </div>
        <small className="num">{fmtEur(lo)} – {fmtEur(hi)}</small>
      </div>
      <svg className="chart" viewBox={`0 0 ${W} ${H}`} preserveAspectRatio="none" role="img"
        aria-label="Стойност на портфейла спрямо задържане на началния BTC">
        <path d={path(bench)} fill="none" stroke="var(--line-bench)" strokeWidth="1.5" strokeDasharray="4 3" vectorEffect="non-scaling-stroke" />
        <path d={path(eq)} fill="none" stroke="var(--line-equity)" strokeWidth="2" vectorEffect="non-scaling-stroke" />
      </svg>
      <div className="row" style={{ justifyContent: "space-between" }}>
        <small>{fmtTime(points[0].ts)}</small>
        <small>{fmtTime(points[points.length - 1].ts)}</small>
      </div>
    </figure>
  );
}
