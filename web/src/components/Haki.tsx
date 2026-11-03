import { useId, useMemo } from "react";

/* Decorative "Conqueror's Haki" art: black lightning with crimson edges crackling out of a point,
 * with shockwave rings. Bolts come from a seeded generator, so the art is the same on every load. */

function seeded(seed: number) {
  return () => {
    seed = (seed + 0x6d2b79f5) | 0;
    let t = Math.imul(seed ^ (seed >>> 15), 1 | seed);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

type Bolt = { d: string; w: number };

/** A jagged bolt from (x, y) heading along `angle`, forking into thinner bolts as it goes. */
function bolt(r: () => number, x: number, y: number, angle: number, len: number, w: number, depth: number, out: Bolt[]) {
  const steps = 6 + Math.floor(r() * 4);
  const step = len / steps;
  const pts = [[x, y]];
  for (let i = 1; i <= steps; i++) {
    const a = angle + (r() - 0.5) * 1.1;
    x += Math.cos(a) * step;
    y += Math.sin(a) * step;
    pts.push([x, y]);
    if (depth > 0 && i < steps - 1 && r() < 0.3) {
      const fork = angle + (r() < 0.5 ? -1 : 1) * (0.35 + r() * 0.5);
      bolt(r, x, y, fork, len * (0.3 + r() * 0.25), w * 0.55, depth - 1, out);
    }
  }
  out.push({ d: "M" + pts.map((p) => `${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join("L"), w });
}

function Bolts({ bolts, glow }: { bolts: Bolt[]; glow: string }) {
  return (
    <>
      <g filter={`url(#${glow})`} stroke="var(--haki-glow)" className="haki-flash">
        {bolts.map((b, i) => <path key={i} d={b.d} strokeWidth={b.w * 3.2} />)}
      </g>
      <g stroke="var(--haki-core)">
        {bolts.map((b, i) => <path key={i} d={b.d} strokeWidth={b.w} />)}
      </g>
      <g stroke="var(--haki-spark)" className="haki-flash late">
        {bolts.filter((b) => b.w > 2).map((b, i) => <path key={i} d={b.d} strokeWidth={b.w * 0.22} />)}
      </g>
    </>
  );
}

function Defs({ id }: { id: string }) {
  return (
    <defs>
      <filter id={`${id}g`} x="-50%" y="-50%" width="200%" height="200%">
        <feGaussianBlur stdDeviation="3.5" />
      </filter>
      <radialGradient id={`${id}a`}>
        <stop offset="0" stopColor="var(--haki-glow)" stopOpacity=".38" />
        <stop offset=".45" stopColor="var(--haki-violet)" stopOpacity=".14" />
        <stop offset="1" stopColor="var(--haki-violet)" stopOpacity="0" />
      </radialGradient>
      <radialGradient id={`${id}c`}>
        <stop offset="0" stopColor="var(--haki-core)" />
        <stop offset=".55" stopColor="var(--haki-core)" stopOpacity=".85" />
        <stop offset="1" stopColor="var(--haki-core)" stopOpacity="0" />
      </radialGradient>
    </defs>
  );
}

/** A burst of black lightning radiating from the center, for heroes and empty states. */
export function HakiBurst({ seed = 7, bolts: count = 13, className = "" }: { seed?: number; bolts?: number; className?: string }) {
  const id = useId().replace(/:/g, "");
  const bolts = useMemo(() => {
    const r = seeded(seed);
    const out: Bolt[] = [];
    for (let i = 0; i < count; i++) {
      const a = (i / count) * Math.PI * 2 + (r() - 0.5) * 0.45;
      bolt(r, Math.cos(a) * 26, Math.sin(a) * 26, a, 150 + r() * 120, 3.2 + r() * 2.4, 2, out);
    }
    return out;
  }, [seed, count]);
  return (
    <svg className={`haki burst ${className}`} viewBox="-300 -300 600 600" aria-hidden focusable="false">
      <Defs id={id} />
      <circle r="300" fill={`url(#${id}a)`} />
      <g fill="none" stroke="var(--haki-glow)" strokeWidth="2">
        {[0, 1, 2].map((i) => <circle key={i} r="210" className="haki-wave" style={{ animationDelay: `${i * 1.2}s` }} />)}
      </g>
      <g fill="none" strokeLinecap="round" strokeLinejoin="round">
        <Bolts bolts={bolts} glow={`${id}g`} />
      </g>
      <circle r="44" fill={`url(#${id}c)`} />
    </svg>
  );
}

/** Lightning crackling in from the right edge, behind a page heading. */
export function HakiStrip({ seed = 3 }: { seed?: number }) {
  const id = useId().replace(/:/g, "");
  const bolts = useMemo(() => {
    const r = seeded(seed);
    const out: Bolt[] = [];
    for (let i = 0; i < 7; i++) {
      const a = Math.PI + (i / 6 - 0.5) * 1.3 + (r() - 0.5) * 0.2;
      bolt(r, 610, 80, a, 220 + r() * 260, 2.2 + r() * 2, 2, out);
    }
    return out;
  }, [seed]);
  return (
    <svg className="haki strip" viewBox="0 0 640 160" preserveAspectRatio="xMaxYMid slice" aria-hidden focusable="false">
      <Defs id={id} />
      <linearGradient id={`${id}f`} x1="0" x2="1">
        <stop offset=".3" stopColor="#fff" stopOpacity="0" />
        <stop offset=".85" stopColor="#fff" stopOpacity=".9" />
      </linearGradient>
      <mask id={`${id}m`}><rect width="640" height="160" fill={`url(#${id}f)`} /></mask>
      <g mask={`url(#${id}m)`}>
        <circle cx="610" cy="80" r="200" fill={`url(#${id}a)`} />
        <g fill="none" stroke="var(--haki-glow)" strokeWidth="1.5" transform="translate(610 80)">
          {[0, 1].map((i) => <circle key={i} r="150" className="haki-wave" style={{ animationDelay: `${i * 1.8}s` }} />)}
        </g>
        <g fill="none" strokeLinecap="round" strokeLinejoin="round">
          <Bolts bolts={bolts} glow={`${id}g`} />
        </g>
      </g>
    </svg>
  );
}
