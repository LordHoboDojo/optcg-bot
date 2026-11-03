// Decks made in the deck builder, kept in this browser, and the deck rules it checks as you build.
// The rules are optcg/deck.py's; the server checks a deck again before a game is played with it.

import type { Card, DeckCards } from "./api";

export const DECK_SIZE = 50;
export const MAX_COPIES = 4;

/** What the Play page shows for a deck without loading the card pool. */
export type Face = { name: string; colors: string[]; art: string | null };
export type Built = { id: string; name: string; leader: string | null; face: Face | null;
  cards: Record<string, number>; updated: number };

const KEY = "optcg.decks";

/** The saved decks, most recently changed first. Storage can be off (a private window, blocked
 * site data), so this falls back to none. */
export function loadDecks(): Built[] {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) ?? "[]");
    return Array.isArray(v) ? v.filter((d) => d && typeof d.id === "string" && d.cards).sort((a, b) => b.updated - a.updated) : [];
  } catch {
    return [];
  }
}

/** False when the browser won't keep them. */
export function saveDecks(decks: Built[]): boolean {
  try {
    localStorage.setItem(KEY, JSON.stringify(decks));
    return true;
  } catch {
    return false;
  }
}

export function blank(name = "Untitled deck"): Built {
  return { id: Date.now().toString(36) + Math.random().toString(36).slice(2, 8), name, leader: null, face: null, cards: {}, updated: 0 };
}

export const face = (c: Card): Face => ({ name: c.name, colors: c.colors, art: c.art });
export const size = (d: Built) => Object.values(d.cards).reduce((a, b) => a + b, 0);
export const shares = (c: Card, leader: Card) => c.colors.some((x) => leader.colors.includes(x));
export const maxCopies = (c: Card) => (c.unlimited ? DECK_SIZE : MAX_COPIES);

/** Why the deck can't be played yet; empty when it can. */
export function problems(d: Built, pool: Map<string, Card>): string[] {
  const out: string[] = [];
  const leader = d.leader ? pool.get(d.leader) : undefined;
  if (!leader) out.push("Choose a Leader.");
  const n = size(d);
  if (n !== DECK_SIZE) out.push(`The deck has ${n} cards; it needs ${DECK_SIZE}.`);
  for (const [id, k] of Object.entries(d.cards)) {
    const c = pool.get(id);
    if (!c) out.push(`${id} isn't a card this site knows.`);
    else if (c.category === "Leader") out.push(`${c.name} is a Leader and can't be in the deck.`);
    else if (leader && !shares(c, leader)) out.push(`${c.name} (${id}) shares no color with your Leader.`);
    if (c && k > maxCopies(c)) out.push(`${k} copies of ${c.name}; at most ${MAX_COPIES}.`);
  }
  return out;
}

/** The list as the server reads it ("4xOP01-016" per line, the Leader included). */
export function wire(d: Built): string {
  return [`1x${d.leader}`, ...Object.entries(d.cards).map(([id, n]) => `${n}x${id}`)].join("\n");
}

/** The list for people, like the files in decks/: "4 OP08-051 Buckin". */
export function toText(d: Built, pool: Map<string, Card>): string {
  const line = (id: string, n: number) => `${n} ${id} ${pool.get(id)?.name ?? ""}`.trim();
  const leader = d.leader ? pool.get(d.leader) : undefined;
  return [`# ${d.name}`, ...(d.leader ? [line(d.leader, 1)] : []),
    ...sorted(d, pool).map(([c, n]) => line(c.id, n))].join("\n") + (leader ? "" : "\n# no Leader yet");
}

/** Read a decklist: "4xOP01-016", "4 OP01-016 Kaido" or "OP01-016 x4" per line, # for comments,
 * as optcg/carddb.py's parse_decklist does. The Leader is the one Leader card in it. */
export function parseList(text: string, pool: Map<string, Card>): DeckCards {
  const counts: Record<string, number> = {};
  for (const raw of text.split("\n")) {
    const line = raw.split("#")[0].trim();
    if (!line) continue;
    const m = line.match(/^(\d+)\s*x?\s*([A-Z0-9]+-\d{3})\S*(?:\s+\D.*)?$/);
    const r = m ? null : line.match(/^([A-Z0-9]+-\d{3})\S*\s*x?\s*(\d+)$/);
    if (!m && !r) throw new Error(`Can't read the line "${line}".`);
    const [id, n] = m ? [m[2], +m[1]] : [r![1], +r![2]];
    if (!pool.has(id)) throw new Error(`${id} isn't a card this site knows.`);
    counts[id] = (counts[id] ?? 0) + n;
  }
  const leaders = Object.keys(counts).filter((id) => pool.get(id)?.category === "Leader");
  if (leaders.length !== 1) throw new Error(`A list needs exactly one Leader; this one has ${leaders.length}.`);
  delete counts[leaders[0]];
  return { name: "", leader: leaders[0], cards: counts };
}

const ORDER = ["Character", "Event", "Stage"];

/** The deck's cards in the order a list shows them: Characters, Events, Stages, each by cost. */
export function sorted(d: Built, pool: Map<string, Card>): [Card, number][] {
  return Object.entries(d.cards).flatMap(([id, n]) => {
    const c = pool.get(id);
    return c ? [[c, n] as [Card, number]] : [];
  }).sort(([a], [b]) => ORDER.indexOf(a.category) - ORDER.indexOf(b.category) || (a.cost ?? 0) - (b.cost ?? 0) ||
    a.name.localeCompare(b.name));
}
