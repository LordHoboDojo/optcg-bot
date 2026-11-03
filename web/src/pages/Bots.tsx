import { useEffect, useState, type FormEvent } from "react";
import { Link } from "react-router-dom";
import { api, date, when, type Bots as BotsData } from "../api";
import { Loading, PageHead, SignInPrompt } from "../components/Layout";
import { useSession } from "../session";

function Copy({ text }: { text: string }) {
  const [done, setDone] = useState(false);
  return (
    <button className="btn small" onClick={async () => { await navigator.clipboard.writeText(text); setDone(true); setTimeout(() => setDone(false), 1500); }}>
      {done ? "Copied" : "Copy"}
    </button>
  );
}

export default function Bots() {
  const { me } = useSession();
  const [data, setData] = useState<BotsData | null>(null);
  const [name, setName] = useState("");
  const [error, setError] = useState("");
  const [fresh, setFresh] = useState<{ bot: string; key: string } | null>(null);

  const load = () => api.bots().then(setData).catch((e) => setError(e.message));
  useEffect(() => { if (me?.user) load(); }, [me?.user?.id]);

  if (me === null) return <><PageHead title="Your bots" /><Loading /></>;
  if (!me.user) return <><PageHead title="Your bots" /><SignInPrompt what="register a bot" /></>;
  if (!data) return <><PageHead title="Your bots" />{error ? <div className="notice bad">{error}</div> : <Loading />}</>;

  async function add(e: FormEvent) {
    e.preventDefault();
    setError("");
    try { await api.addBot(name.trim()); setName(""); await load(); }
    catch (err) { setError(err instanceof Error ? err.message : String(err)); }
  }
  async function newKey(id: number, bot: string) {
    const { key } = await api.newKey(id);
    setFresh({ bot, key });
    await load();
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
  async function revoke(id: number) {
    if (!confirm("Revoke this key? Bots using it can no longer connect.")) return;
    await api.revokeKey(id);
    await load();
  }
  async function retire(id: number, bot: string) {
    if (!confirm(`Retire ${bot}? Its keys stop working; its matches stay in your history.`)) return;
    await api.retireBot(id);
    await load();
  }

  const url = `${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/bot/v1/play`;
  const opponent = data.opponents[0] ?? "greedy";

  return (
    <>
      <PageHead title="Your bots">Register a bot, give it an API key, and it can play the house bots over the <Link to="/docs/bot-api">bot API</Link>.</PageHead>

      {fresh && (
        <div className="key-reveal" style={{ marginBottom: 24 }}>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <b>New API key for {fresh.bot}</b>
            <button className="btn small ghost" onClick={() => setFresh(null)}>Done</button>
          </div>
          <code>{fresh.key}</code>
          <div className="row"><Copy text={fresh.key} /><span className="small muted">Copy it now: it's shown only once. Only a hash is stored.</span></div>
          <pre className="code" style={{ marginTop: 14 }}>{`OPTCG_KEY=${fresh.key} python tools/bot_client.py \\\n  --url ${url} --opponent "${opponent}" -v`}</pre>
        </div>
      )}

      <div className="grid" style={{ gap: 16 }}>
        {data.bots.map((b) => (
          <div key={b.id} className="card pad">
            <div className="row" style={{ justifyContent: "space-between" }}>
              <div>
                <h3 style={{ fontSize: 18 }}>{b.name}</h3>
                <div className="tiny">Registered {date(b.created)} · {b.record.played} matches · {b.record.won} won</div>
              </div>
              <div className="row">
                <button className="btn small primary" onClick={() => newKey(b.id, b.name)}>New API key</button>
                <button className="btn small danger" onClick={() => retire(b.id, b.name)}>Retire</button>
              </div>
            </div>
            <div className="table-wrap" style={{ marginTop: 14 }}>
              {b.keys.length === 0 ? <p className="small muted">No active keys.</p> : (
                <table className="data">
                  <thead><tr><th>Key</th><th>Created</th><th /></tr></thead>
                  <tbody>{b.keys.map((k) => (
                    <tr key={k.id}><td><code>{k.prefix}…</code></td><td>{date(k.created)}</td>
                      <td style={{ textAlign: "right" }}><button className="btn small ghost danger" onClick={() => revoke(k.id)}>Revoke</button></td></tr>
                  ))}</tbody>
                </table>
              )}
            </div>
          </div>
        ))}

        <form className="card pad" onSubmit={add}>
          <h3>Register a bot</h3>
          <p className="small muted" style={{ margin: "4px 0 12px" }}>3 to 32 letters, digits, - or _. Names are unique across the site. Up to {data.limit} bots.</p>
          <div className="row">
            <input type="text" value={name} onChange={(e) => setName(e.target.value)} placeholder="my-bot" aria-label="Bot name"
              pattern="[A-Za-z0-9_\-]{3,32}" required style={{ flex: 1, width: "auto", minWidth: 200 }} />
            <button className="btn primary">Register</button>
          </div>
          {error && <div className="notice bad" style={{ marginTop: 12 }}>{error}</div>}
        </form>
      </div>

      <section className="section">
        <h2>Recent matches</h2>
        <p className="sub">Your bots' matches against the house bots. Replays show the game from your bot's seat.</p>
        <div className="card table-wrap">
          {data.matches.length === 0 ? (
            <div className="empty"><h3>No matches yet</h3><p>Connect with a key to play one. <Link to="/docs/bot-api">How it works</Link></p></div>
          ) : (
            <table className="data">
              <thead><tr><th>Started</th><th>Bot</th><th>Opponent</th><th>Turns</th><th>Result</th><th /></tr></thead>
              <tbody>{data.matches.map((m) => (
                <tr key={m.id}>
                  <td>{when(m.started)}</td><td>{m.bot}</td><td>{m.opponent}</td><td>{m.turns ?? "—"}</td>
                  <td>{m.result ? <span className={`badge ${m.result === "win" ? "good" : m.result === "loss" ? "bad" : ""}`}>
                    {m.result[0].toUpperCase() + m.result.slice(1)}</span> : <span className="badge">Playing</span>}
                    {m.reason && m.reason !== "normal" && <span className="tiny"> · {m.reason.replace("_", " ")}</span>}</td>
                  <td style={{ textAlign: "right" }}>{m.ended && <a href={`/matches/${m.id}`}>Replay</a>}</td>
                </tr>
              ))}</tbody>
            </table>
          )}
        </div>
      </section>
    </>
  );
}
