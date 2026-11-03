import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, type Options } from "../api";
import { useTitle } from "../components/Layout";
import { useSession } from "../session";

const Icon = ({ d }: { d: string }) => (
  <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"
    strokeLinecap="round" strokeLinejoin="round" aria-hidden><path d={d} /></svg>
);

export default function Home() {
  const { me } = useSession();
  const [opts, setOpts] = useState<Options | null>(null);
  useEffect(() => { api.options().then(setOpts).catch(() => {}); }, []);
  const art = (opts?.decks ?? []).filter((d) => d.art);
  const pick = (names: string[]) => names.map((n) => art.find((d) => d.leader.includes(n))).filter(Boolean);
  const hero = [...pick(["Charlotte Linlin", "Rocks", "Monkey.D.Luffy"]), ...art].slice(0, 3);
  const best = [...(opts?.bots ?? [])].sort((a, b) => (b.rating ?? 0) - (a.rating ?? 0));
  const game = me?.game;
  useTitle("");

  return (
    <>
      {game && !game.over && (
        <div className="card resume">
          <div>
            <b>You have a game in progress</b>
            <div className="small muted">{game.deck} vs {game.bot} ({game.bot_deck}) · turn {game.turns}</div>
          </div>
          <span className="spacer" />
          <a className="btn primary" href="/game">Resume game</a>
        </div>
      )}
      <section className="hero">
        <div>
          <div className="eyebrow">ONE PIECE Card Game · bots</div>
          <h1>Play against bots trained to win.</h1>
          <p className="lead">
            A full rules engine with every card's stats and most effects, house bots trained by reinforcement learning
            and search, and an API to pit your own bot against them.
          </p>
          <div className="row">
            <Link className="btn primary big" to="/play">Play now</Link>
            <Link className="btn big" to="/docs/bot-api">Connect your bot</Link>
          </div>
          <p className="tiny" style={{ marginTop: 14 }}>No account needed to play. Sign in to register bots.</p>
        </div>
        <div className="hero-art" aria-hidden>
          {hero.map((d) => d && <img key={d.name} src={d.art!} alt="" />)}
        </div>
      </section>

      <section className="section grid cols-3">
        <div className="card feature">
          <div className="icon"><Icon d="M5 3l14 9-14 9V3z" /></div>
          <h3>Play in your browser</h3>
          <p>Pick any of {opts ? opts.decks.length : "100+"} decks and a house bot. Drag cards onto the table, and scrub
            back through the whole game while you play.</p>
          <Link to="/play">Choose your decks →</Link>
        </div>
        <div className="card feature">
          <div className="icon"><Icon d="M8 9l-4 3 4 3M16 9l4 3-4 3M14 5l-4 14" /></div>
          <h3>Bring your own bot</h3>
          <p>Your program connects over a WebSocket, sees exactly what its seat may see, and answers each decision.
            Every match is saved as a replay.</p>
          <Link to="/docs/bot-api">Read the bot API →</Link>
        </div>
        <div className="card feature">
          <div className="icon"><Icon d="M4 20V10M10 20V4M16 20v-7M22 20H2" /></div>
          <h3>See how the models compare</h3>
          <p>Ratings from thousands of games between every pair of bots, head-to-head records, and how each training
            run improved.</p>
          <Link to="/analysis">Open the analysis →</Link>
        </div>
      </section>

      {best.length > 0 && (
        <section className="section">
          <h2>House bots</h2>
          <p className="sub">Who you can play. Ratings are Elo-style, with the hand-written greedy bot at 1500.</p>
          <div className="grid cols-3">
            {best.map((b, i) => (
              <div key={b.name} className="card pad">
                <div className="row" style={{ justifyContent: "space-between" }}>
                  <h3>{b.name}</h3>
                  {i === 0 && b.rating && <span className="badge accent">Strongest</span>}
                </div>
                <div className="row small muted" style={{ marginTop: 6, gap: 8 }}>
                  <span className="badge">{b.kind}</span>
                  {b.rating && <span>Rating <b style={{ color: "var(--ink)" }}>{b.rating}</b></span>}
                </div>
                {b.about && <p className="small muted" style={{ marginTop: 10 }}>{b.about}</p>}
              </div>
            ))}
          </div>
        </section>
      )}
    </>
  );
}
