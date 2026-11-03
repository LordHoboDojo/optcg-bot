import { useEffect, useMemo, useState } from "react";
import { Link, useSearchParams } from "react-router-dom";
import { api, COLORS, pct, type Deck, type Options } from "../api";
import { Loading, PageHead } from "../components/Layout";
import { DECK_SIZE, loadDecks, size, wire } from "../decks";
import { useSession } from "../session";

type Slot = "mine" | "theirs";
const BUILT = "built:";  // the key of a deck from the deck builder, before its id
const COLOR_FILTERS = ["red", "green", "blue", "purple", "black", "yellow"];

function Dots({ colors }: { colors: string[] }) {
  return <>{colors.map((c) => <span key={c} className="dot" style={{ background: COLORS[c] ?? "#999" }} title={c} />)}</>;
}

function DeckArt({ deck, className }: { deck?: Deck; className: string }) {
  return deck?.art ? <img className={className} src={deck.art} alt="" loading="lazy" />
    : <div className={`${className} ph`}>{deck?.leader ?? ""}</div>;
}

export default function Play() {
  const { me } = useSession();
  const [params] = useSearchParams();
  const built = useMemo(() => loadDecks().filter((b) => b.leader && b.face), []);
  // the builder's decks as the deck grid shows them, keyed "built:<id>"
  const builtDecks = useMemo(() => built.map((b): Deck => ({ name: BUILT + b.id, leader: b.face!.name,
    leader_id: b.leader!, colors: b.face!.colors, art: b.face!.art, coverage: 1 })), [built]);
  const [opts, setOpts] = useState<Options | null>(null);
  const [mine, setMine] = useState("");
  const [theirs, setTheirs] = useState("");
  const [bot, setBot] = useState("");
  const [slot, setSlot] = useState<Slot>("mine");
  const [query, setQuery] = useState("");
  const [color, setColor] = useState<string | null>(null);
  const [complete, setComplete] = useState(false);
  const [error, setError] = useState("");
  const [starting, setStarting] = useState(false);

  useEffect(() => {
    api.options().then((o) => {
      setOpts(o);
      const asked = params.get("deck");
      setMine(asked && (asked.startsWith(BUILT) ? built.some((b) => BUILT + b.id === asked) : o.decks.some((d) => d.name === asked))
        ? asked : o.default_deck);
      setTheirs(o.house_deck);
      setBot([...o.bots].sort((a, b) => (b.rating ?? 0) - (a.rating ?? 0))[0]?.name ?? "");
    }).catch((e) => setError(String(e.message ?? e)));
  }, []);

  const byName = useMemo(() => new Map([...builtDecks, ...(opts?.decks ?? [])].map((d) => [d.name, d])), [opts, builtDecks]);
  const builtById = useMemo(() => new Map(built.map((b) => [BUILT + b.id, b])), [built]);
  /** What to call a deck: a built deck's own name, else the site's. */
  const title = (name: string) => builtById.get(name)?.name.trim() || (builtById.has(name) ? "Untitled deck" : name);
  const shown = useMemo(() => [...(slot === "mine" ? builtDecks : []), ...(opts?.decks ?? [])].filter((d) =>
    (!query || `${title(d.name)} ${d.leader}`.toLowerCase().includes(query.toLowerCase())) &&
    (!color || d.colors.includes(color)) && (!complete || d.coverage >= 1)), [opts, builtDecks, builtById, slot, query, color, complete]);

  const chosen = slot === "mine" ? mine : theirs;
  const choose = (name: string) => (slot === "mine" ? setMine : setTheirs)(name);
  const mineBuilt = builtById.get(mine);
  const unfinished = mineBuilt && size(mineBuilt) !== DECK_SIZE;

  async function start() {
    setStarting(true);
    setError("");
    try {
      const { url } = mineBuilt ? await api.newGame(title(mine), theirs, bot, wire(mineBuilt)) : await api.newGame(mine, theirs, bot);
      window.location.assign(url);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setStarting(false);
    }
  }

  if (!opts) return <><PageHead title="New game" />{error ? <div className="notice bad">{error}</div> : <Loading height={480} />}</>;

  const Summary = (
    <div className="card summary stack">
      <h3>Your game</h3>
      <button className={`slot ${slot === "mine" ? "active" : ""}`} onClick={() => setSlot("mine")}>
        <DeckArt deck={byName.get(mine)} className="ph" />
        <div><div className="who-label">You play</div><div className="deck-name">{title(mine)}</div></div>
      </button>
      <button className={`slot ${slot === "theirs" ? "active" : ""}`} onClick={() => setSlot("theirs")}>
        <DeckArt deck={byName.get(theirs)} className="ph" />
        <div><div className="who-label">The bot plays</div><div className="deck-name">{theirs}</div></div>
      </button>
      <div>
        <div className="who-label tiny" style={{ fontWeight: 600, margin: "6px 0 8px", textTransform: "uppercase", letterSpacing: ".05em" }}>Opponent</div>
        <div className="bots" role="radiogroup" aria-label="Opponent">
          {opts.bots.map((b) => (
            <button key={b.name} role="radio" aria-checked={bot === b.name} className={`bot ${bot === b.name ? "sel" : ""}`}
              onClick={() => setBot(b.name)}>
              <span className="radio" />
              <span>
                <b>{b.name}</b>
                <span className="small muted"> · {b.kind}{b.rating ? ` · ${b.rating}` : ""}</span>
                {b.about && <span className="tiny" style={{ display: "block", marginTop: 2 }}>{b.about}</span>}
              </span>
            </button>
          ))}
        </div>
      </div>
      {unfinished && <div className="notice warn">Your deck has {size(mineBuilt)} of {DECK_SIZE} cards. <Link to={`/builder?deck=${mineBuilt.id}`}>Finish it in the deck builder</Link>.</div>}
      {error && <div className="notice bad">{error}</div>}
      <button className="btn primary big" style={{ width: "100%" }} disabled={starting || !mine || !theirs || !bot || unfinished} onClick={start}>
        {starting ? "Setting up the table…" : "Start game"}
      </button>
      {me?.game && !me.game.over && <p className="tiny">Starting a new game ends the one in progress.</p>}
      {!me?.user && <p className="tiny">Playing as a guest. Sign in to keep your game history.</p>}
    </div>
  );

  return (
    <>
      <PageHead title="New game" actions={<Link className="btn" to="/builder">Build a deck</Link>}>Choose your deck and the bot's, pick an opponent, and start. Seats are drawn at random and the game decides who goes first.</PageHead>
      <div className="play">
        <div>
          <div className="row" style={{ justifyContent: "space-between" }}>
            <div className="tabs" role="tablist">
              <button role="tab" aria-selected={slot === "mine"} className={slot === "mine" ? "on" : ""} onClick={() => setSlot("mine")}>Your deck</button>
              <button role="tab" aria-selected={slot === "theirs"} className={slot === "theirs" ? "on" : ""} onClick={() => setSlot("theirs")}>Bot's deck</button>
            </div>
            <span className="small muted">{shown.length} of {opts.decks.length + (slot === "mine" ? builtDecks.length : 0)} decks</span>
          </div>
          <div className="filters">
            <input type="search" placeholder="Search decks or leaders" value={query} onChange={(e) => setQuery(e.target.value)} aria-label="Search decks" />
            {COLOR_FILTERS.map((c) => (
              <button key={c} className={`chip ${color === c ? "on" : ""}`} onClick={() => setColor(color === c ? null : c)} aria-pressed={color === c}>
                <span className="dot" style={{ background: COLORS[c] }} />{c[0].toUpperCase() + c.slice(1)}
              </button>
            ))}
            <button className={`chip ${complete ? "on" : ""}`} onClick={() => setComplete(!complete)} aria-pressed={complete}
              title="Only decks whose every effect is implemented">Fully implemented</button>
          </div>
          <div className="decks">
            {shown.map((d) => (
              <button key={d.name} className={`deck ${chosen === d.name ? "sel" : ""}`} onClick={() => choose(d.name)} aria-pressed={chosen === d.name}>
                <DeckArt deck={d} className="art" />
                <span className="tag">{builtById.has(d.name) ? <span className="badge accent" title="Made in the deck builder">Yours</span>
                  : d.coverage >= 1 ? <span className="badge good">Full</span> : <span className="badge" title="Share of the deck's effect text the engine implements">{pct(d.coverage)}</span>}</span>
                <div className="meta">
                  <div className="n">{title(d.name)}</div>
                  <div className="c"><Dots colors={d.colors} /><span className="tiny" style={{ marginLeft: 4 }}>{d.leader}</span></div>
                </div>
              </button>
            ))}
            {shown.length === 0 && <div className="empty" style={{ gridColumn: "1 / -1" }}>No decks match.</div>}
          </div>
        </div>
        {Summary}
      </div>
    </>
  );
}
