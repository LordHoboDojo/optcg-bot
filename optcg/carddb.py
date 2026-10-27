"""Real card data: load data/cards.json (built by tools/fetch_cards.py) into CardDefs,
and build decks from decklists or the official recommended decks."""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

from .cards import CardDef, Category
from .deck import Deck, DeckError
from .effect_text import compile_card, effect_lines
from .scripts import SCRIPTS

DATA = Path(__file__).resolve().parent.parent / "data"


def card_from_json(j: dict) -> CardDef:
    comp = compile_card(j["effect"], j["trigger"])
    fields = dict(
        id=j["id"], name=j["name"], category=Category(j["category"]),
        colors=tuple(j["colors"]), cost=j["cost"] or 0, power=j["power"] or 0,
        counter=j["counter"] or 0, life=j["life"] or 0,
        types=tuple(j["types"]), attributes=tuple(j["attributes"]),
        keywords=frozenset(comp.keywords), effects=tuple(comp.effects),
        static_power=comp.static_power_fn(), static_keywords=comp.static_keywords_fn(),
        static_cost=comp.static_cost_fn(), hand_cost=comp.hand_cost_fn(),
        text=j["effect"], trigger_text=j["trigger"], unimplemented=tuple(comp.unimplemented),
    )
    if s := SCRIPTS.get(j["id"]):
        extra = s.build()
        added = tuple(extra.pop("effects", ()))
        if s.covers is None:
            fields["effects"], fields["unimplemented"] = added, ()
        else:
            fields["effects"] += added
            fields["unimplemented"] = tuple(u for u in fields["unimplemented"]
                                            if not any(c in u for c in s.covers))
        fields.update(extra)
    return CardDef(**fields)


@lru_cache(maxsize=None)
def load_cards(path: str | None = None) -> dict[str, CardDef]:
    p = Path(path) if path else DATA / "cards.json"
    if not p.exists():
        raise FileNotFoundError(f"{p} not found; run: python3 tools/fetch_cards.py")
    return {j["id"]: card_from_json(j) for j in json.loads(p.read_text())}


def deck_from_counts(leader_id: str, counts: dict[str, int], cards: dict[str, CardDef] | None = None) -> Deck:
    cards = cards or load_cards()
    missing = [i for i in [leader_id, *counts] if i not in cards]
    if missing:
        raise DeckError(f"unknown card ids: {missing}")
    return Deck(cards[leader_id], [cards[i] for i, n in counts.items() for _ in range(n)])


def parse_decklist(text: str, cards: dict[str, CardDef] | None = None) -> Deck:
    """Parse lines like '4xOP01-016', '4 OP01-016', or 'OP01-016 x4'. The Leader is the
    one Leader card in the list."""
    cards = cards or load_cards()
    counts: dict[str, int] = {}
    for line in text.splitlines():
        line = line.split("#")[0].strip()
        if not line:
            continue
        m = re.fullmatch(r"(\d+)\s*x?\s*([A-Z0-9]+-\d{3})\S*", line) or \
            re.fullmatch(r"([A-Z0-9]+-\d{3})\S*\s*x?\s*(\d+)", line)
        if not m:
            raise DeckError(f"can't read decklist line: {line!r}")
        n, cid = (m.group(1), m.group(2)) if m.group(1).isdigit() else (m.group(2), m.group(1))
        counts[cid] = counts.get(cid, 0) + int(n)
    leaders = [i for i in counts if i in cards and cards[i].category == Category.LEADER]
    if len(leaders) != 1:
        raise DeckError(f"decklist needs exactly 1 Leader, found {leaders}")
    leader = leaders[0]
    del counts[leader]
    return deck_from_counts(leader, counts, cards)


def official_decks(cards: dict[str, CardDef] | None = None) -> dict[str, Deck]:
    """The official recommended decks that pass deck validation, by name."""
    cards = cards or load_cards()
    out = {}
    for d in json.loads((DATA / "decks.json").read_text()):
        try:
            deck = deck_from_counts(d["leader"], d["cards"], cards)
            deck.validate()
        except DeckError:
            continue
        name, k = d["name"], 2
        while name in out:  # the site reuses some deck names
            name, k = f"{d['name']} #{k}", k + 1
        out[name] = deck
    return out


def find_deck(name: str, decks: dict[str, Deck] | None = None) -> Deck:
    decks = decks or official_decks()
    hits = [k for k in decks if name.lower() in k.lower()]
    exact = [k for k in hits if k.lower() == name.lower()]
    if exact:
        return decks[exact[0]]
    if len(hits) != 1:
        raise KeyError(f"{name!r} matches {len(hits)} decks: {hits[:10]}")
    return decks[hits[0]]


def coverage(deck: Deck) -> tuple[int, int, list[str]]:
    """(implemented effect lines, total effect lines, unimplemented lines) counting every copy."""
    total = done = 0
    missing = []
    for d in [deck.leader, *deck.cards]:
        lines = effect_lines(d.text + "\n" + d.trigger_text)
        total += len(lines)
        done += len(lines) - len(d.unimplemented)
        missing += [f"{d.id} {d.name}: {u}" for u in d.unimplemented]
    return done, total, missing
