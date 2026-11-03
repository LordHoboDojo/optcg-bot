// The server's JSON API (optcg/server/app.py).

export type User = { id: number; name: string | null; email: string | null; since: number };
export type CurrentGame = { over: boolean; bot: string; deck: string; bot_deck: string; turns: number };
export type Me = { user: User | null; signin: "google" | "dev" | null; game: CurrentGame | null };

export type Deck = { name: string; leader: string; leader_id: string; colors: string[]; art: string | null; coverage: number };
export type HouseBot = { name: string; kind: string; rating: number | null; about: string | null; analysis_name: string | null };
export type Options = { decks: Deck[]; bots: HouseBot[]; house_deck: string; default_deck: string };

export type Category = "Leader" | "Character" | "Event" | "Stage";
export type Card = { id: string; name: string; category: Category; colors: string[]; cost: number | null;
  power: number | null; counter: number | null; life: number | null; types: string[]; attributes: string[];
  rarity: string; set: string; text: string; trigger: string; unlimited: boolean; art: string | null; full: string | null };
export type DeckCards = { name: string; leader: string; cards: Record<string, number> };

export type Record_ = { played: number; won: number };
export type Game = { bot: string; deck: string; bot_deck: string; turns: number; result: "win" | "loss" | "draw";
  reason: string; ended: number };
export type Account = { user: User; totals: Record_; games: Game[] };

export type Key = { id: number; prefix: string; created: number };
export type Bot = { id: number; name: string; created: number; record: Record_; keys: Key[] };
export type Match = { id: string; bot: string; opponent: string; started: number; ended: number | null;
  reason: string | null; turns: number | null; result: "win" | "loss" | "draw" | null };
export type Bots = { bots: Bot[]; matches: Match[]; limit: number; opponents: string[] };

export type Pair = { a: string; b: string; win: number; draw: number; games: number; ci: [number, number];
  by_deck: Record<string, number> };
export type Run = { name: string; about: string; train: [number, number][]; eval: [number, number, number][] };
export type AnalysisBot = { name: string; spec: string; about: string; rating: number };
export type AnalysisData = { generated: string; matchup: [string, string]; games_per_pair: number;
  bots: AnalysisBot[]; pairs: Pair[]; runs: Run[] };
export type Analysis = { analysis: AnalysisData | null; house: Record<string, { people?: Record_; bots?: Record_ }>;
  bots: HouseBot[] };

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string) { super(message); }
}

async function call<T>(method: string, path: string, body?: unknown): Promise<T> {
  const res = await fetch(path, {
    method,
    headers: body === undefined ? {} : { "Content-Type": "application/json" },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  const text = await res.text();
  const data = text ? JSON.parse(text) : {};
  if (!res.ok) throw new ApiError(res.status, data.error ?? "error", data.detail ?? data.error ?? res.statusText);
  return data as T;
}

export const api = {
  me: () => call<Me>("GET", "/api/me"),
  options: () => call<Options>("GET", "/api/options"),
  newGame: (deck: string, bot_deck: string, bot: string, decklist?: string) =>
    call<{ url: string }>("POST", "/api/games", { deck, bot_deck, bot, decklist }),
  cards: () => call<{ cards: Card[] }>("GET", "/api/cards"),
  deck: (name: string) => call<DeckCards>("GET", `/api/deck?name=${encodeURIComponent(name)}`),
  account: () => call<Account>("GET", "/api/account"),
  rename: (name: string) => call("PATCH", "/api/account", { name }),
  deleteAccount: () => call("DELETE", "/api/account"),
  logout: () => call("POST", "/api/logout", {}),
  bots: () => call<Bots>("GET", "/api/bots"),
  addBot: (name: string) => call<{ id: number }>("POST", "/api/bots", { name }),
  retireBot: (id: number) => call("DELETE", `/api/bots/${id}`),
  newKey: (botId: number) => call<{ key: string }>("POST", `/api/bots/${botId}/keys`, {}),
  revokeKey: (id: number) => call("DELETE", `/api/keys/${id}`),
  analysis: () => call<Analysis>("GET", "/api/analysis"),
  docs: () => fetch("/api/docs/bot-api").then((r) => r.text()),
};

export const COLORS: Record<string, string> = {
  red: "#d7263d", green: "#1f9d55", blue: "#2a78d6", purple: "#7b4bd6", black: "#2b2b2e", yellow: "#f2c200",
};

export const pct = (x: number, digits = 0) => `${(x * 100).toFixed(digits)}%`;
export const date = (t: number) => new Date(t * 1000).toLocaleDateString(undefined, { year: "numeric", month: "short", day: "numeric" });
export const when = (t: number) => new Date(t * 1000).toLocaleString(undefined, { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
