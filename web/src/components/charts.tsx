// Charts for the Analysis page, drawn as plain SVG. Colors come from the CSS tokens in styles.css
// (validated categorical slots --s1..--s4, and a blue/red diverging pair with a gray midpoint).
import { useMemo, useRef, useState, type ReactNode } from "react";
import { pct } from "../api";

type Tip = { x: number; y: number; body: ReactNode } | null;

function useTip() {
  const [tip, setTip] = useState<Tip>(null);
  const box = useRef<HTMLDivElement>(null);
  const show = (e: { clientX: number; clientY: number }, body: ReactNode) => {
    const r = box.current!.getBoundingClientRect();
    const x = Math.min(e.clientX - r.left + 14, r.width - 170);
    setTip({ x: Math.max(4, x), y: e.clientY - r.top + 14, body });
  };
  const el = tip && <div className="tip" style={{ left: tip.x, top: tip.y }}>{tip.body}</div>;
  return { box, show, hide: () => setTip(null), el };
}

/** Ratings as bars from the greedy baseline (1500): right of it beats greedy on average, left loses. */
export function RatingBars({ rows, baseline = 1500, mark }: {
  rows: { name: string; rating: number; about: string }[]; baseline?: number; mark?: Set<string>;
}) {
  const { box, show, hide, el } = useTip();
  const sorted = [...rows].sort((a, b) => b.rating - a.rating);
  const lo = Math.min(baseline, ...sorted.map((r) => r.rating)) - 60;
  const hi = Math.max(baseline, ...sorted.map((r) => r.rating)) + 60;
  const W = 720, L = 110, R = 64, rowH = 34, H = sorted.length * rowH + 28;
  const x = (v: number) => L + ((v - lo) / (hi - lo)) * (W - L - R);
  const ticks = [];
  for (let t = Math.ceil(lo / 250) * 250; t <= hi; t += 250) ticks.push(t);
  return (
    <div className="chart" ref={box} onMouseLeave={hide}>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Ratings of each bot">
        {ticks.map((t) => (
          <g key={t}>
            <line x1={x(t)} x2={x(t)} y1={0} y2={H - 22} stroke="var(--grid)" />
            <text x={x(t)} y={H - 6} textAnchor="middle" fontSize="12" fill="var(--ink-3)">{t}</text>
          </g>
        ))}
        {sorted.map((r, i) => {
          const y = i * rowH + 6, x0 = x(baseline), x1 = x(r.rating), up = r.rating >= baseline;
          const w = Math.max(2, Math.abs(x1 - x0));
          return (
            <g key={r.name} onMouseMove={(e) => show(e, <><div className="tv">{r.rating}</div><div className="tl">{r.name}</div>
              <div className="tiny" style={{ maxWidth: 220 }}>{r.about}</div></>)} tabIndex={0}
              onFocus={(e) => { const b = (e.target as Element).getBoundingClientRect(); show({ clientX: b.left + b.width / 2, clientY: b.top }, <><div className="tv">{r.rating}</div><div className="tl">{r.name}</div></>); }}
              onBlur={hide}>
              <rect x={0} y={y - 4} width={W} height={rowH} fill="transparent" />
              <text x={L - 12} y={y + 15} textAnchor="end" fontSize="13" fontWeight={mark?.has(r.name) ? 700 : 500} fill="var(--ink)">{r.name}</text>
              <rect x={Math.min(x0, x1)} y={y + 3} width={w} height={rowH - 14} rx={4}
                fill={up ? "var(--div-pos)" : "var(--div-neg)"} />
              <text x={up ? x1 + 8 : x1 - 8} y={y + 15} textAnchor={up ? "start" : "end"} fontSize="13" fontWeight="600"
                fill="var(--ink-2)">{r.rating}</text>
            </g>
          );
        })}
        <line x1={x(baseline)} x2={x(baseline)} y1={0} y2={H - 22} stroke="var(--axis)" strokeWidth="1.5" />
      </svg>
      {el}
    </div>
  );
}

function mix(a: string, b: string, t: number) {
  const p = (h: string) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
  const [x, y] = [p(a), p(b)];
  return `rgb(${x.map((v, i) => Math.round(v + (y[i] - v) * t)).join(",")})`;
}

function ink(rgb: string) {
  const [r, g, b] = rgb.match(/\d+/g)!.map(Number).map((v) => { v /= 255; return v <= .03928 ? v / 12.92 : ((v + .055) / 1.055) ** 2.4; });
  return .2126 * r + .7152 * g + .0722 * b > .36 ? "#0b0b0b" : "#ffffff";
}

/** Win share of the row bot against the column bot. */
export function HeadToHead({ names, share, detail, dark }: {
  names: string[]; share: (a: string, b: string) => number | null; detail: (a: string, b: string) => ReactNode; dark: boolean;
}) {
  const { box, show, hide, el } = useTip();
  const pos = dark ? "#3987e5" : "#2a78d6", neg = dark ? "#e66767" : "#e34948", mid = dark ? "#383835" : "#f0efec";
  const color = (v: number) => (v >= .5 ? mix(mid, pos, Math.min(1, (v - .5) / .5)) : mix(mid, neg, Math.min(1, (.5 - v) / .5)));
  return (
    <div className="chart" ref={box} onMouseLeave={hide}>
      <div className="table-wrap">
        <div className="heat" style={{ gridTemplateColumns: `96px repeat(${names.length}, minmax(52px, 1fr))`, minWidth: 96 + names.length * 56 }}
          role="table" aria-label="Head-to-head win rates: row bot against column bot">
          <div role="row" style={{ display: "contents" }}>
            <div className="h col tiny" role="columnheader">row vs →</div>
            {names.map((n) => <div key={n} className="h col" role="columnheader">{n}</div>)}
          </div>
          {names.map((a) => (
            <div key={a} role="row" style={{ display: "contents" }}>
              <div className="h" role="rowheader">{a}</div>
              {names.map((b) => {
                const v = a === b ? null : share(a, b);
                if (v === null) return <div key={b} className="cell self" role="cell">—</div>;
                const bg = color(v);
                return (
                  <div key={b} className="cell" role="cell" tabIndex={0} style={{ background: bg, color: ink(bg) }}
                    onMouseMove={(e) => show(e, detail(a, b))}
                    onFocus={(e) => { const r = e.currentTarget.getBoundingClientRect(); show({ clientX: r.right, clientY: r.top }, detail(a, b)); }}
                    onBlur={hide} aria-label={`${a} beats ${b} ${pct(v)} of games`}>{pct(v)}</div>
                );
              })}
            </div>
          ))}
        </div>
      </div>
      <div className="scale">
        <span>Row loses</span>
        <span className="bar" style={{ background: `linear-gradient(90deg, ${neg}, ${mid}, ${pos})` }} />
        <span>Row wins</span>
      </div>
      {el}
    </div>
  );
}

const SERIES = ["var(--s1)", "var(--s2)", "var(--s3)", "var(--s4)"];

/** Win rate against greedy over training, one line per run, with a crosshair readout. */
export function TrainingCurves({ runs }: { runs: { name: string; points: [number, number][] }[] }) {
  const { box, show, hide, el } = useTip();
  const [hover, setHover] = useState<number | null>(null);
  const W = 720, H = 300, L = 44, R = 84, T = 12, B = 30;
  const maxX = Math.max(1, ...runs.flatMap((r) => r.points.map((p) => p[0])));
  const x = (v: number) => L + (v / maxX) * (W - L - R);
  const y = (v: number) => T + (1 - v) * (H - T - B);
  const xs = useMemo(() => [...new Set(runs.flatMap((r) => r.points.map((p) => p[0])))].sort((a, b) => a - b), [runs]);
  // The run's point nearest `at`, or none when `at` is past either end of that run
  const near = (pts: [number, number][], at: number) => !pts.length || at < pts[0][0] - 5 || at > pts[pts.length - 1][0] + 5 ? null
    : pts.reduce((best, p) => (Math.abs(p[0] - at) < Math.abs(best[0] - at) ? p : best), pts[0]);
  const xTicks = [0, Math.round(maxX / 4), Math.round(maxX / 2), Math.round((3 * maxX) / 4), maxX];

  function move(e: React.MouseEvent<SVGSVGElement>) {
    const r = e.currentTarget.getBoundingClientRect();
    const vx = ((e.clientX - r.left) / r.width) * W;
    const at = ((vx - L) / (W - L - R)) * maxX;
    const snap = xs.reduce((b, v) => (Math.abs(v - at) < Math.abs(b - at) ? v : b), xs[0]);
    setHover(snap);
    show(e, <>
      <div className="tl" style={{ marginBottom: 4 }}>Iteration {snap}</div>
      {runs.map((run, i) => {
        const p = near(run.points, snap);
        return p && <div key={run.name} className="tr"><span className="row" style={{ gap: 6 }}>
          <span className="key" style={{ background: SERIES[i], width: 12, height: 2, display: "inline-block" }} />
          <span className="tl">{run.name}</span></span><b>{pct(p[1], 1)}</b></div>;
      })}
    </>);
  }

  return (
    <div className="chart" ref={box}>
      <div className="legend">{runs.map((r, i) => <span key={r.name}><i className="key" style={{ background: SERIES[i] }} />{r.name}</span>)}</div>
      <svg viewBox={`0 0 ${W} ${H}`} role="img" aria-label="Win rate against greedy during training" onMouseMove={move}
        onMouseLeave={() => { setHover(null); hide(); }}>
        {[0, .25, .5, .75, 1].map((t) => (
          <g key={t}>
            <line x1={L} x2={W - R} y1={y(t)} y2={y(t)} stroke={t === .5 ? "var(--axis)" : "var(--grid)"} strokeDasharray={t === .5 ? "4 4" : undefined} />
            <text x={L - 8} y={y(t) + 4} textAnchor="end" fontSize="12" fill="var(--ink-3)">{pct(t)}</text>
          </g>
        ))}
        {xTicks.map((t) => <text key={t} x={x(t)} y={H - 8} textAnchor="middle" fontSize="12" fill="var(--ink-3)">{t}</text>)}
        <line x1={L} x2={W - R} y1={y(0)} y2={y(0)} stroke="var(--axis)" />
        {runs.map((r, i) => (
          <g key={r.name}>
            <polyline fill="none" stroke={SERIES[i]} strokeWidth="2" strokeLinejoin="round" strokeLinecap="round"
              points={r.points.map((p) => `${x(p[0])},${y(p[1])}`).join(" ")} />
            {r.points.length > 0 && (() => {
              const last = r.points[r.points.length - 1];
              return <text x={x(last[0]) + 8} y={y(last[1]) + 4} fontSize="12" fontWeight="600" fill="var(--ink-2)">{r.name}</text>;
            })()}
          </g>
        ))}
        {hover !== null && (
          <g>
            <line x1={x(hover)} x2={x(hover)} y1={T} y2={H - B} stroke="var(--ink-3)" />
            {runs.map((r, i) => { const p = near(r.points, hover); return p && <circle key={r.name} cx={x(p[0])} cy={y(p[1])} r={4.5} fill={SERIES[i]} stroke="var(--chart-surface)" strokeWidth="2" />; })}
          </g>
        )}
        <text x={W - R} y={H - 8} textAnchor="end" fontSize="12" fill="var(--ink-3)" dx={R - 4}>iteration</text>
      </svg>
      {el}
    </div>
  );
}
