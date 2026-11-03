import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { api, COLORS, type Card, type Category, type Options } from "../api";
import { Loading, PageHead } from "../components/Layout";
import { blank, type Built, DECK_SIZE, face, loadDecks, maxCopies, parseList, problems, saveDecks, shares, size,
  sorted, toText } from "../decks";

type Tab = Category | "All";
const TABS: [Tab, string][] = [["Leader", "Leaders"], ["Character", "Characters"], ["Event", "Events"], ["Stage", "Stages"], ["All", "All cards"]];
const COLOR_FILTERS = ["red", "green", "blue", "purple", "black", "yellow"];
const PAGE = 120;  // cards shown at a time

function Dots({ colors }: { colors: string[] }) {
  return <>{colors.map((c) => <span key={c} className="dot" style={{ background: COLORS[c] ?? "#999" }} title={c} />)}</>;
}

function Art({ card, full }: { card: Card; full?: boolean }) {
  const src = full ? card.full ?? card.art : card.art;
  return src ? <img src={src} alt={card.name} loading="lazy" /> : <div className="ph">{card.name}</div>;
}

/** "OP01" for OP01-016: the set a card was printed in, for the set filter. */
const setOf = (c: Card) => c.id.split("-")[0];

export default function DeckBuilder() {
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [pool, setPool] = useState<Card[] | null>(null);
  const [opts, setOpts] = useState<Options | null>(null);
  const [error, setError] = useState("");
  const [decks, setDecks] = useState<Built[]>(loadDecks);
  const [deck, setDeck] = useState<Built>(() => decks.find((d) => d.id === params.get("deck")) ?? decks[0] ?? blank());
  const [unsaved, setUnsaved] = useState(false);
  const [tab, setTab] = useState<Tab>(deck.leader ? "All" : "Leader");
  const [query, setQuery] = useState("");
  const [color, setColor] = useState<string | null>(null);
  const [legal, setLegal] = useState(true);
  const [cost, setCost] = useState("");
  const [set, setSet] = useState("");
  const [shown, setShown] = useState(PAGE);
  const [viewing, setViewing] = useState<Card | null>(null);
  const [listOpen, setListOpen] = useState(false);
  const [listText, setListText] = useState("");
  const [listNote, setListNote] = useState<{ ok: boolean; text: string } | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);

  useEffect(() => {
    api.cards().then((r) => setPool(r.cards)).catch((e) => setError(String(e.message ?? e)));
    api.options().then(setOpts).catch(() => {});  // only for "start from a site deck"
  }, []);
  useEffect(() => { setShown(PAGE); }, [tab, query, color, legal, cost, set]);
  useEffect(() => {
    if (viewing && !dialog.current?.open) dialog.current?.showModal();
    else if (!viewing) dialog.current?.close();
  }, [viewing]);

  const byId = useMemo(() => new Map((pool ?? []).map((c) => [c.id, c])), [pool]);
  const hay = useMemo(() => new Map((pool ?? []).map((c) =>
    [c.id, `${c.id} ${c.name} ${c.types.join(" ")} ${c.text} ${c.trigger}`.toLowerCase()])), [pool]);
  const sets = useMemo(() => [...new Set((pool ?? []).map(setOf))].sort((a, b) => a.localeCompare(b, undefined, { numeric: true })), [pool]);
  const leader = deck.leader ? byId.get(deck.leader) : undefined;
  const n = size(deck);
  const issues = useMemo(() => pool ? problems(deck, byId) : [], [deck, byId, pool]);

  const matches = useMemo(() => {
    const q = query.trim().toLowerCase();
    return (pool ?? []).filter((c) =>
      (tab === "All" ? c.category !== "Leader" : c.category === tab) &&
      (!q || hay.get(c.id)!.includes(q)) && (!color || c.colors.includes(color)) &&
      (!legal || !leader || c.category === "Leader" || shares(c, leader)) &&
      (!cost || String(c.cost ?? 0) === cost) && (!set || setOf(c) === set))
      .sort((a, b) => tab === "Leader" ? a.id.localeCompare(b.id, undefined, { numeric: true })
        : (a.cost ?? 0) - (b.cost ?? 0) || a.id.localeCompare(b.id, undefined, { numeric: true }));
  }, [pool, hay, tab, query, color, legal, leader, cost, set]);

  /** Change the deck and keep it in this browser. */
  function edit(next: Built) {
    next = { ...next, updated: Date.now() };
    const all = [next, ...decks.filter((d) => d.id !== next.id)];
    setDeck(next);
    setDecks(all);
    setUnsaved(!saveDecks(all));
    if (params.get("deck") !== next.id) setParams({ deck: next.id }, { replace: true });
  }

  function open(d: Built) {
    setDeck(d);
    setTab(d.leader ? "All" : "Leader");
    setListOpen(false);
    setParams(d.updated ? { deck: d.id } : {}, { replace: true });
  }

  const count = (c: Card) => deck.cards[c.id] ?? 0;
  /** Why a card can't go in (one more copy), or "" if it can. */
  function blocked(c: Card): string {
    if (!leader) return "Choose a Leader first";
    if (!shares(c, leader)) return "Shares no color with your Leader";
    if (count(c) >= maxCopies(c)) return `At most ${maxCopies(c)} copies`;
    if (n >= DECK_SIZE) return `The deck has ${DECK_SIZE} cards`;
    return "";
  }
  function add(c: Card, k: number) {
    const cards = { ...deck.cards };
    const next = Math.max(0, count(c) + k);
    if (next) cards[c.id] = next;
    else delete cards[c.id];
    edit({ ...deck, cards });
  }
  function chooseLeader(c: Card) {
    const named = deck.name === "Untitled deck" || deck.name === leader?.name;
    edit({ ...deck, leader: c.id, face: face(c), name: named ? c.name : deck.name });
    setTab("All");
    setQuery("");
  }

  function remove() {
    if (!window.confirm(`Delete "${deck.name}" from this browser?`)) return;
    const all = decks.filter((d) => d.id !== deck.id);
    setDecks(all);
    saveDecks(all);
    open(all[0] ?? blank());
  }

  function showList() {
    setListText(toText(deck, byId));
    setListNote(null);
    setListOpen(!listOpen);
  }
  async function copyList() {
    try {
      await navigator.clipboard.writeText(listText);
      setListNote({ ok: true, text: "Copied." });
    } catch {
      setListNote({ ok: false, text: "The browser didn't allow copying; select the list and copy it yourself." });
    }
  }
  /** A new deck with these cards. */
  function make(leaderId: string, cards: Record<string, number>, name: string) {
    const d = { ...blank(name.slice(0, 80)), leader: leaderId, face: face(byId.get(leaderId)!), cards };
    edit(d);
    setTab("All");
    setListText(toText(d, byId));
    setListNote({ ok: true, text: `Made a new deck, "${d.name}".` });
  }
  function loadList(text: string) {
    try {
      const got = parseList(text, byId);
      make(got.leader, got.cards, text.match(/^#\s*(.+)$/m)?.[1]?.trim() || byId.get(got.leader)!.name);
    } catch (e) {
      setListNote({ ok: false, text: e instanceof Error ? e.message : String(e) });
    }
  }
  async function fromSite(name: string) {
    try {
      const d = await api.deck(name);
      make(d.leader, d.cards, `${name} (copy)`);
    } catch (e) {
      setListNote({ ok: false, text: e instanceof Error ? e.message : String(e) });
    }
  }

  if (!pool) return <><PageHead title="Deck builder" />{error ? <div className="notice bad">{error}</div> : <Loading height={480} />}</>;

  const curve = Array.from({ length: 10 }, (_, i) => sorted(deck, byId).filter(([c]) => (c.cost ?? 0) === i + 1)
    .reduce((a, [, k]) => a + k, 0));
  const top = Math.max(1, ...curve);
  const kinds = ["Character", "Event", "Stage"].map((k) => [k, sorted(deck, byId).filter(([c]) => c.category === k)
    .reduce((a, [, x]) => a + x, 0)] as const);

  const Summary = (
    <div className="card summary stack" id="deck">
      <div className="row" style={{ gap: 8, flexWrap: "nowrap" }}>
        <select aria-label="Your decks" value={deck.id} onChange={(e) => open(decks.find((d) => d.id === e.target.value) ?? blank())}>
          {!deck.updated && <option value={deck.id}>{deck.name} (new)</option>}
          {decks.map((d) => <option key={d.id} value={d.id}>{d.name}</option>)}
        </select>
        <button className="btn" onClick={() => open(blank())} title="Start an empty deck">New</button>
      </div>
      <label className="field">Name
        <input type="text" value={deck.name} maxLength={80} onChange={(e) => edit({ ...deck, name: e.target.value })} />
      </label>
      {leader ? (
        <button className="slot active" onClick={() => setViewing(leader)} title="See the Leader">
          <Art card={leader} />
          <div>
            <div className="who-label">Leader</div>
            <div className="deck-name">{leader.name}</div>
            <div className="row tiny" style={{ gap: 4, marginTop: 4 }}><Dots colors={leader.colors} />
              <span style={{ marginLeft: 4 }}>{leader.id} · {leader.life} life · {leader.power} power</span></div>
          </div>
        </button>
      ) : (
        <button className="slot" onClick={() => setTab("Leader")}>
          <div className="ph" />
          <div><div className="who-label">Leader</div><div className="deck-name">Choose one from the Leaders</div></div>
        </button>
      )}

      <div>
        <div className="row" style={{ justifyContent: "space-between" }}>
          <b>{n} / {DECK_SIZE} cards</b>
          <span className="tiny">{kinds.map(([k, x]) => `${x} ${k.toLowerCase()}${x === 1 ? "" : "s"}`).join(" · ")}</span>
        </div>
        <div className={`meter ${n === DECK_SIZE ? "full" : ""}`} role="progressbar" aria-label="Cards in the deck"
          aria-valuemin={0} aria-valuemax={DECK_SIZE} aria-valuenow={n}>
          <div style={{ width: `${Math.min(100, (n / DECK_SIZE) * 100)}%` }} />
        </div>
      </div>

      {n > 0 && (
        <div className="curve" aria-label="Cards by cost">
          {curve.map((k, i) => (
            <div key={i} className="col" title={`${k} card${k === 1 ? "" : "s"} costing ${i + 1}`}>
              <span className="tiny">{k || ""}</span>
              <div className="b" style={{ height: `${(k / top) * 52}px` }} />
              <span className="tiny">{i + 1}</span>
            </div>
          ))}
        </div>
      )}

      {n > 0 && (
        <div className="decklist" aria-label="Cards in the deck">
          {sorted(deck, byId).map(([c, k]) => (
            <div key={c.id} className="line">
              <span className="cost" title="Cost">{c.cost ?? 0}</span>
              <button className="nm" onClick={() => setViewing(c)} title={`${c.name} (${c.id})`}>{c.name}</button>
              <button className="step" onClick={() => add(c, -1)} aria-label={`Remove a ${c.name}`}>−</button>
              <b className="k">{k}</b>
              <button className="step" onClick={() => add(c, 1)} disabled={!!blocked(c)} title={blocked(c) || undefined}
                aria-label={`Add a ${c.name}`}>+</button>
            </div>
          ))}
        </div>
      )}

      {issues.length ? (
        <div className="notice warn" aria-live="polite">
          <ul className="issues">{issues.slice(0, 6).map((p) => <li key={p}>{p}</li>)}</ul>
          {issues.length > 6 && <div className="tiny">and {issues.length - 6} more</div>}
        </div>
      ) : <div className="notice good" aria-live="polite">Ready to play.</div>}
      {unsaved && <div className="notice bad">This browser isn't keeping your decks (private window or blocked site data). Copy the list to keep it.</div>}

      <button className="btn primary big" style={{ width: "100%" }} disabled={issues.length > 0}
        onClick={() => navigate(`/play?deck=built:${deck.id}`)}>Play this deck</button>
      <div className="row" style={{ gap: 8 }}>
        <button className="btn small" onClick={showList} aria-expanded={listOpen}>Copy or paste a list</button>
        {deck.updated > 0 && <button className="btn small ghost danger" onClick={remove}>Delete deck</button>}
      </div>
      {listOpen && (
        <div className="stack">
          <textarea className="list" aria-label="Decklist" rows={10} value={listText} spellCheck={false}
            onChange={(e) => setListText(e.target.value)} />
          <div className="row" style={{ gap: 8 }}>
            <button className="btn small" onClick={copyList}>Copy</button>
            <button className="btn small" onClick={() => loadList(listText)}>Make a deck from this list</button>
          </div>
          {opts && (
            <select aria-label="Start from a site deck" value="" onChange={(e) => e.target.value && fromSite(e.target.value)}>
              <option value="">Start from a site deck…</option>
              {opts.decks.map((d) => <option key={d.name} value={d.name}>{d.name}</option>)}
            </select>
          )}
          {listNote && <div className={`notice ${listNote.ok ? "good" : "bad"}`}>{listNote.text}</div>}
          <p className="tiny">One card per line, like <code>4 OP01-016 Nami</code> or <code>4xOP01-016</code>, the Leader included.</p>
        </div>
      )}
      <p className="tiny">Decks are kept in this browser. Copy the list to move one to another device.</p>
    </div>
  );

  return (
    <>
      <PageHead title="Deck builder" actions={<Link className="btn" to="/play">Play a site deck</Link>}>
        Pick a Leader, then add 50 cards that share a color with it, up to 4 of each. Click a card's picture to read it.
      </PageHead>
      <div className="builder">
        <div>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <div className="tabs" role="tablist">
              {TABS.map(([t, label]) => (
                <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>{label}</button>
              ))}
            </div>
            <span className="small muted">{matches.length} cards</span>
          </div>
          <div className="filters">
            <input type="search" placeholder="Search names, types or text" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search cards" />
            <select aria-label="Cost" value={cost} onChange={(e) => setCost(e.target.value)} style={{ width: "auto" }}>
              <option value="">Any cost</option>
              {Array.from({ length: 10 }, (_, i) => <option key={i} value={i + 1}>Cost {i + 1}</option>)}
            </select>
            <select aria-label="Set" value={set} onChange={(e) => setSet(e.target.value)} style={{ width: "auto" }}>
              <option value="">Any set</option>
              {sets.map((s) => <option key={s} value={s}>{s}</option>)}
            </select>
            {COLOR_FILTERS.map((c) => (
              <button key={c} className={`chip ${color === c ? "on" : ""}`} onClick={() => setColor(color === c ? null : c)} aria-pressed={color === c}>
                <span className="dot" style={{ background: COLORS[c] }} />{c[0].toUpperCase() + c.slice(1)}
              </button>
            ))}
            {leader && tab !== "Leader" && (
              <button className={`chip ${legal ? "on" : ""}`} onClick={() => setLegal(!legal)} aria-pressed={legal}
                title="Only cards that share a color with your Leader">Leader's colors</button>
            )}
          </div>
          <div className="pool">
            {matches.slice(0, shown).map((c) => {
              const k = count(c);
              const chosen = c.category === "Leader" && deck.leader === c.id;
              return (
                <div key={c.id} className={`tile ${k || chosen ? "in" : ""}`}>
                  <button className="face" onClick={() => setViewing(c)} aria-label={`Read ${c.name} (${c.id})`}><Art card={c} /></button>
                  {k > 0 && <span className="n" aria-label={`${k} in the deck`}>{k}</span>}
                  <div className="meta">
                    <div className="nm" title={c.name}>{c.name}</div>
                    <div className="row" style={{ gap: 4, flexWrap: "nowrap" }}>
                      <span className="tiny id">{c.id}</span>
                      {c.category === "Leader" ? (
                        <button className="btn small" disabled={chosen} onClick={() => chooseLeader(c)}>{chosen ? "Chosen" : "Choose"}</button>
                      ) : <>
                        <button className="step" onClick={() => add(c, -1)} disabled={!k} aria-label={`Remove a ${c.name}`}>−</button>
                        <button className="step" onClick={() => add(c, 1)} disabled={!!blocked(c)} title={blocked(c) || undefined}
                          aria-label={`Add a ${c.name}`}>+</button>
                      </>}
                    </div>
                  </div>
                </div>
              );
            })}
            {matches.length === 0 && <div className="empty" style={{ gridColumn: "1 / -1" }}>No cards match.</div>}
          </div>
          {matches.length > shown && (
            <div className="row" style={{ justifyContent: "center", marginTop: 20 }}>
              <button className="btn" onClick={() => setShown(shown + PAGE)}>Show more ({matches.length - shown} left)</button>
            </div>
          )}
          <a className="builder-bar" href="#deck">
            <span>{leader?.name ?? "No Leader yet"}</span><b>{n} / {DECK_SIZE}</b><span>See the deck ↓</span>
          </a>
        </div>
        {Summary}
      </div>

      <dialog ref={dialog} className="cardview" onClose={() => setViewing(null)}
        onClick={(e) => { if (e.target === dialog.current) setViewing(null); }}>
        {viewing && (
          <div className="in">
            <Art card={viewing} full />
            <div className="stack">
              <div>
                <h2>{viewing.name}</h2>
                <div className="row tiny" style={{ gap: 6, marginTop: 6 }}>
                  <Dots colors={viewing.colors} /><span>{viewing.category} · {viewing.id} · {viewing.rarity} · {viewing.set}</span>
                </div>
              </div>
              <div className="row small" style={{ gap: 16 }}>
                {viewing.category === "Leader" ? <span><b>{viewing.life}</b> life</span> : <span><b>{viewing.cost ?? 0}</b> cost</span>}
                {viewing.power != null && <span><b>{viewing.power}</b> power</span>}
                {viewing.counter != null && <span><b>+{viewing.counter}</b> counter</span>}
              </div>
              {viewing.types.length > 0 && <div className="small muted">{viewing.types.join(" / ")}</div>}
              {viewing.text && <p className="text">{viewing.text}</p>}
              {viewing.trigger && <p className="text"><b>Trigger</b> {viewing.trigger}</p>}
              <div className="row">
                {viewing.category === "Leader" ? (
                  <button className="btn primary" disabled={deck.leader === viewing.id}
                    onClick={() => { chooseLeader(viewing); setViewing(null); }}>{deck.leader === viewing.id ? "Your Leader" : "Choose as Leader"}</button>
                ) : <>
                  <button className="step" onClick={() => add(viewing, -1)} disabled={!count(viewing)} aria-label={`Remove a ${viewing.name}`}>−</button>
                  <b>{count(viewing)} in the deck</b>
                  <button className="step" onClick={() => add(viewing, 1)} disabled={!!blocked(viewing)} title={blocked(viewing) || undefined}
                    aria-label={`Add a ${viewing.name}`}>+</button>
                  {blocked(viewing) && count(viewing) < maxCopies(viewing) && <span className="tiny">{blocked(viewing)}</span>}
                </>}
                <span className="spacer" />
                <button className="btn" onClick={() => setViewing(null)}>Close</button>
              </div>
            </div>
          </div>
        )}
      </dialog>
    </>
  );
}
