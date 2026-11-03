import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { api, date, pct, when, type Account as AccountData } from "../api";
import { initials, Loading, PageHead, SignInPrompt } from "../components/Layout";
import { useSession } from "../session";

const RESULT = { win: "good", loss: "bad", draw: "" } as const;

export default function Account() {
  const { me, refresh } = useSession();
  const [data, setData] = useState<AccountData | null>(null);
  const [name, setName] = useState("");
  const [editing, setEditing] = useState(false);
  const [msg, setMsg] = useState<{ kind: "good" | "bad"; text: string } | null>(null);
  const [confirm, setConfirm] = useState("");

  const load = () => api.account().then((a) => { setData(a); setName(a.user.name ?? ""); });
  useEffect(() => { if (me?.user) load(); }, [me?.user?.id]);

  if (me === null) return <><PageHead title="Account" /><Loading /></>;
  if (!me.user) return <><PageHead title="Account" /><SignInPrompt what="see your account" /></>;
  if (!data) return <><PageHead title="Account" /><Loading /></>;

  const { user, totals, games } = data;
  async function save() {
    try {
      await api.rename(name);
      await Promise.all([load(), refresh()]);
      setEditing(false);
      setMsg({ kind: "good", text: "Name saved." });
    } catch (e) { setMsg({ kind: "bad", text: e instanceof Error ? e.message : String(e) }); }
  }
  async function signOut() { await api.logout(); await refresh(); window.location.assign("/"); }
  async function remove() {
    await api.deleteAccount();
    await refresh();
    window.location.assign("/");
  }

  return (
    <>
      <PageHead title="Account" actions={<button className="btn" onClick={signOut}>Sign out</button>} />
      <div className="grid cols-2" style={{ alignItems: "start" }}>
        <div className="card pad">
          <div className="row" style={{ gap: 16 }}>
            <span className="avatar" style={{ width: 52, height: 52, fontSize: 18 }} aria-hidden>{initials(user.name || user.email)}</span>
            <div style={{ minWidth: 0 }}>
              <h2 style={{ fontSize: 20 }}>{user.name || "Unnamed"}</h2>
              <div className="small muted">{user.email}</div>
              <div className="tiny">Member since {date(user.since)}</div>
            </div>
          </div>
          <div style={{ marginTop: 20 }}>
            {editing ? (
              <div className="row">
                <input type="text" value={name} maxLength={40} onChange={(e) => setName(e.target.value)} aria-label="Display name" style={{ flex: 1, width: "auto" }} />
                <button className="btn primary" onClick={save}>Save</button>
                <button className="btn ghost" onClick={() => { setEditing(false); setName(user.name ?? ""); }}>Cancel</button>
              </div>
            ) : <button className="btn small" onClick={() => setEditing(true)}>Change display name</button>}
            {msg && <div className={`notice ${msg.kind}`} style={{ marginTop: 12 }}>{msg.text}</div>}
          </div>
        </div>
        <div className="grid" style={{ gridTemplateColumns: "repeat(3, 1fr)" }}>
          <div className="card stat"><div className="l">Games</div><div className="v">{totals.played}</div></div>
          <div className="card stat"><div className="l">Wins</div><div className="v">{totals.won}</div></div>
          <div className="card stat"><div className="l">Win rate</div><div className="v">{totals.played ? pct(totals.won / totals.played) : "—"}</div></div>
        </div>
      </div>

      <section className="section">
        <div className="row" style={{ justifyContent: "space-between", marginBottom: 12 }}>
          <h2>Recent games</h2>
          <Link className="btn small" to="/play">New game</Link>
        </div>
        <div className="card table-wrap">
          {games.length === 0 ? (
            <div className="empty"><h3>No finished games yet</h3><p>Games you finish while signed in show up here.</p></div>
          ) : (
            <table className="data">
              <thead><tr><th>Finished</th><th>Your deck</th><th>Opponent</th><th>Turns</th><th>Result</th></tr></thead>
              <tbody>
                {games.map((g, i) => (
                  <tr key={i}>
                    <td>{when(g.ended)}</td>
                    <td>{g.deck}</td>
                    <td>{g.bot} <span className="tiny">({g.bot_deck})</span></td>
                    <td>{g.turns}</td>
                    <td><span className={`badge ${RESULT[g.result]}`}>{g.result === "win" ? "Win" : g.result === "loss" ? "Loss" : "Draw"}</span>
                      {g.reason === "concede" && <span className="tiny"> · conceded</span>}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </section>

      <section className="section">
        <h2>Bots and API keys</h2>
        <p className="sub">Register bots and manage the keys they connect with.</p>
        <Link className="btn" to="/bots">Manage your bots</Link>
      </section>

      <section className="section">
        <h2>Delete account</h2>
        <p className="sub">Removes your account, your bots and their keys, their match records and replays, and your game history. This can't be undone.</p>
        <div className="card pad row" style={{ alignItems: "end" }}>
          <label className="field" style={{ flex: 1, minWidth: 220 }}>Type DELETE to confirm
            <input type="text" value={confirm} onChange={(e) => setConfirm(e.target.value)} />
          </label>
          <button className="btn danger solid" disabled={confirm !== "DELETE"} onClick={remove}>Delete my account</button>
        </div>
      </section>
    </>
  );
}
