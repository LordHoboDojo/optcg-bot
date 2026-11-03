import { useEffect, useMemo, useState } from "react";
import { api, pct, type Analysis as AnalysisResp } from "../api";
import { HeadToHead, RatingBars, TrainingCurves } from "../components/charts";
import { Loading, PageHead } from "../components/Layout";

function useDark() {
  const q = window.matchMedia("(prefers-color-scheme: dark)");
  const [dark, setDark] = useState(q.matches);
  useEffect(() => { const f = () => setDark(q.matches); q.addEventListener("change", f); return () => q.removeEventListener("change", f); }, []);
  return dark;
}

export default function Analysis() {
  const [data, setData] = useState<AnalysisResp | null>(null);
  const [error, setError] = useState("");
  const [view, setView] = useState<"eval" | "train">("eval");
  const [table, setTable] = useState(false);
  const dark = useDark();
  useEffect(() => { api.analysis().then(setData).catch((e) => setError(e.message)); }, []);

  const a = data?.analysis;
  const pairs = useMemo(() => new Map((a?.pairs ?? []).map((p) => [`${p.a}|${p.b}`, p])), [a]);
  if (error) return <><PageHead title="Analysis" /><div className="notice bad">{error}</div></>;
  if (!data) return <><PageHead title="Analysis" /><Loading height={420} /></>;
  if (!a) return <><PageHead title="Analysis" /><div className="card empty"><h3>No analysis yet</h3><p>Run tools/analysis.py to compare the bots.</p></div></>;

  const house = new Set(data.bots.map((b) => b.analysis_name).filter(Boolean) as string[]);
  const names = [...a.bots].sort((x, y) => y.rating - x.rating).map((b) => b.name);
  const lookup = (x: string, y: string) => {
    const p = pairs.get(`${x}|${y}`);
    if (p) return { win: p.win, draw: p.draw, games: p.games, ci: p.ci };
    const q = pairs.get(`${y}|${x}`);
    return q ? { win: 1 - q.win - q.draw, draw: q.draw, games: q.games, ci: [1 - q.ci[1], 1 - q.ci[0]] as [number, number] } : null;
  };
  const best = a.bots.reduce((m, b) => (b.rating > m.rating ? b : m), a.bots[0]);
  const top = names.length > 1 ? lookup(names[0], names[1]) : null;
  const runs = a.runs.map((r) => ({ name: r.name, points: (view === "eval" ? r.eval.map((p) => [p[0], p[1]] as [number, number]) : r.train) }));
  const totalGames = a.pairs.reduce((s, p) => s + p.games, 0);
  const people = Object.entries(data.house).filter(([, v]) => v.people || v.bots);

  return (
    <>
      <PageHead title="Analysis">
        How the bots compare. Every pair played {a.games_per_pair} games of {a.matchup.join(" vs ")}, alternating seats so both
        bots play both decks. Networks play their best-scored move. Updated {a.generated}.
      </PageHead>

      <div className="grid cols-3">
        <div className="card stat"><div className="l">Strongest bot</div><div className="v">{best.name}</div>
          <div className="tiny">rating {best.rating}</div></div>
        <div className="card stat"><div className="l">{names[0]} vs {names[1]}</div><div className="v">{top ? pct(top.win) : "—"}</div>
          <div className="tiny">{top && `95% interval ${pct(top.ci[0])}–${pct(top.ci[1])}`}</div></div>
        <div className="card stat"><div className="l">Games played for this page</div><div className="v">{totalGames.toLocaleString()}</div>
          <div className="tiny">{a.pairs.length} pairs of {a.bots.length} bots</div></div>
      </div>

      <section className="section">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div><h2>Ratings</h2><p className="sub" style={{ marginTop: 6 }}>Elo-style, fitted to every pair's results (Bradley–Terry), with greedy fixed at 1500. 400 points is about 10-to-1 odds.{house.size > 0 && " Bold names are house bots you can play."}</p></div>
          <button className="btn small" onClick={() => setTable(!table)} aria-pressed={table}>{table ? "Show chart" : "Show table"}</button>
        </div>
        {table ? (
          <div className="card table-wrap">
            <table className="data">
              <thead><tr><th>Bot</th><th>Rating</th><th>What it is</th></tr></thead>
              <tbody>{[...a.bots].sort((x, y) => y.rating - x.rating).map((b) => (
                <tr key={b.name}><td><b>{b.name}</b>{house.has(b.name) && <span className="badge accent" style={{ marginLeft: 8 }}>House bot</span>}</td>
                  <td>{b.rating}</td><td className="muted">{b.about}</td></tr>))}</tbody>
            </table>
          </div>
        ) : <RatingBars rows={a.bots} mark={house} />}
      </section>

      <section className="section">
        <h2>Head to head</h2>
        <p className="sub">Share of games the row bot won against the column bot. Hover a cell for the games, the 95% interval and each deck.</p>
        <HeadToHead names={names} dark={dark} share={(x, y) => lookup(x, y)?.win ?? null} detail={(x, y) => {
          const r = lookup(x, y);
          const p = pairs.get(`${x}|${y}`);
          return r && <>
            <div className="tv">{pct(r.win, 1)}</div>
            <div className="tl">{x} vs {y} · {r.games} games</div>
            <div className="tiny">95% interval {pct(r.ci[0])}–{pct(r.ci[1])}</div>
            {p && Object.entries(p.by_deck).map(([deck, w]) => <div key={deck} className="tiny">as {deck}: {pct(w)}</div>)}
          </>;
        }} />
      </section>

      <section className="section">
        <div className="row" style={{ justifyContent: "space-between" }}>
          <div><h2>Training progress</h2><p className="sub" style={{ marginTop: 6 }}>Win rate against greedy as each run trained.</p></div>
          <div className="tabs" role="tablist">
            <button role="tab" aria-selected={view === "eval"} className={view === "eval" ? "on" : ""} onClick={() => setView("eval")}>Evaluations</button>
            <button role="tab" aria-selected={view === "train"} className={view === "train" ? "on" : ""} onClick={() => setView("train")}>Every iteration</button>
          </div>
        </div>
        <TrainingCurves runs={runs} />
        <p className="tiny" style={{ marginTop: 8 }}>
          {view === "eval" ? "Evaluations: 200–300 games with the best-scored move, every few iterations."
            : "Every iteration: about 100 games while training, with sampled (exploring) moves, so lower and noisier."}
        </p>
        <div className="grid cols-2" style={{ marginTop: 16 }}>
          {a.runs.map((r) => <div key={r.name} className="small"><b>{r.name}</b> <span className="muted">— {r.about}</span></div>)}
        </div>
      </section>

      <section className="section">
        <h2>House bots in the wild</h2>
        <p className="sub">Live results on this site: the house bots against people in the browser and against registered bots.</p>
        <div className="card table-wrap">
          {people.length === 0 ? <div className="empty"><h3>No finished games yet</h3><p>Results appear here as people and bots play.</p></div> : (
            <table className="data">
              <thead><tr><th>House bot</th><th>vs people</th><th>vs registered bots</th></tr></thead>
              <tbody>{people.map(([name, v]) => (
                <tr key={name}><td><b>{name}</b></td>
                  <td>{v.people ? `${v.people.won} of ${v.people.played} won (${pct(v.people.won / v.people.played)})` : "—"}</td>
                  <td>{v.bots ? `${v.bots.won} of ${v.bots.played} won (${pct(v.bots.won / v.bots.played)})` : "—"}</td></tr>))}</tbody>
            </table>
          )}
        </div>
      </section>
    </>
  );
}
