"""Record a game and write it as a self-contained HTML slideshow.

    python3 -m optcg.replay --deck0 "(Blue) Rocks.D.Xebec" --deck1 "(Yellow) Charlotte Linlin" --p1 greedy --open
    .venv/bin/python -m optcg.replay --p0 runs/ppo/latest.pt --p1 greedy --seed 7 --out replays/net_vs_greedy.html

One frame per decision: the board when the decision was asked (both hands visible), the options,
the one chosen, and the log lines since the previous frame. A last frame shows the end of the game.
Games run on the Rust engine (engine-rs/), which carries out every card.

Card art comes from StreamingAssets/ next to the project, linked by relative path rather than
embedded, so a replay shows art only while it can still reach that folder. Without it the viewer
draws text cards instead.
"""
from __future__ import annotations

import argparse
import json
import os
import webbrowser
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .agents import Agent
from .cards import CANNOT_ATTACK, KEYWORDS, CardDef, Category
from .deck import Deck
from .play import MAX_STEPS, make_agent, resolve_deck

VIEWER = Path(__file__).with_name("replay_viewer.html")
ASSETS = Path(__file__).resolve().parent.parent / "StreamingAssets"


@lru_cache(maxsize=4)
def art_files(assets: Path = ASSETS) -> tuple[str, ...]:
    """The images a table can use (card art, card backs, playmats, the table), relative to `assets`.
    A server whose art lives elsewhere (e.g. on a CDN) keeps this list in a file instead."""
    if not (assets / "Cards").is_dir():
        return ()
    found = [*(assets / "Cards").glob("*/*"), *(assets / "CardBacks").glob("*"),
             *(assets / "Playmats").glob("*.png"), assets / "background.jpg"]
    return tuple(sorted(f.relative_to(assets).as_posix() for f in found if f.is_file()))


@lru_cache(maxsize=4)
def _art_index(files: tuple[str, ...]) -> dict[str, str]:
    """Card id -> full-size image path."""
    out = {}
    for f in files:
        parts = f.split("/")
        stem, _, ext = parts[-1].rpartition(".")
        if len(parts) == 3 and parts[0] == "Cards" and ext in ("png", "jpg") and not stem.endswith("_small"):
            out.setdefault(stem, f)
    return out


COLOR_ORDER = ("red", "green", "blue", "purple", "black", "yellow")


def _playmat(files: frozenset[str], colors) -> Optional[str]:
    """The official playmat for a Leader's colours (Playmats/BlueYellow.png, ...), if there is one."""
    cols = sorted(colors, key=lambda c: COLOR_ORDER.index(c) if c in COLOR_ORDER else 99)
    for name in ("".join(c.title() for c in cols), "".join(c.title() for c in reversed(cols))):
        if f"Playmats/{name}.png" in files:
            return f"Playmats/{name}.png"
    return None


def art(replay: dict, assets: Path = ASSETS, files: Optional[tuple[str, ...]] = None) -> dict:
    """Image paths for a replay's cards, card backs, playmats and table, relative to `assets`
    (or taken from `files`, a list made by art_files)."""
    files = art_files(assets) if files is None else files
    if not files:
        return {}
    index, have = _art_index(files), frozenset(files)
    leaders = [pl.get("id") for pl in replay["players"]]
    return {"cards": {i: index[i] for i in replay["cards"] if i in index},
            "back": "CardBacks/CardBackRegular.png", "don": "Cards/Don/Don.png",
            "donBack": "CardBacks/CardBackDon.png",
            "mats": [_playmat(have, replay["cards"][i]["col"]) if i in replay["cards"] else None for i in leaders],
            "table": "background.jpg" if "background.jpg" in have else None}


def new_game(decks: tuple[Deck, Deck], seed: Optional[int] = None):
    """A Rust engine game (optcg_rs.Game) with log lines and option labels, for showing."""
    from .env import rust_available, rust_game
    if not rust_available():
        raise SystemExit("replays and the browser game run on the Rust engine; build it first: "
                         "cd engine-rs && maturin develop --release")
    try:
        return rust_game(decks, seed, fast=False)
    except ValueError as e:  # e.g. the sample decks, made-up cards only the Python engine has
        raise ValueError(f"{e}: the Rust engine plays the real cards in data/cards.json") from None


def card_ids(decks: tuple[Deck, Deck]) -> list[str]:
    """Card id by uid: the engine numbers each player's Leader and then its deck, in list order."""
    return [c.id for d in decks for c in (d.leader, *d.cards)]


def card_defs(decks: tuple[Deck, Deck]) -> dict[str, CardDef]:
    return {c.id: c for d in decks for c in (d.leader, *d.cards)}


def pending(g) -> Optional[dict]:
    """The decision the game waits on: {"player", "kind", "prompt", "options"}, where each option is
    [kind, card uid, target uid, index, label]; None once the game is over."""
    j = g.pending_json()
    return json.loads(j) if j else None


def first_player(frames: list[dict]) -> int:
    """Who went first: the turn player of turn 1 (0, as the engine has it, before then)."""
    return next((f["tp"] for f in frames if f["t"] >= 1), 0)


def _keywords(kws: list[str]) -> list[str]:
    return [k for k in sorted(KEYWORDS) if k in kws] + (["Can't attack"] if CANNOT_ATTACK in kws else [])


def _field(defs: dict[str, CardDef], row: list) -> dict:
    uid, cid, power, cost, rested, don, kws = row
    cat = defs[cid].category
    out = {"u": uid, "i": cid}
    if cat in (Category.LEADER, Category.CHARACTER):
        out["p"] = power
    if cat != Category.LEADER:
        out["c"] = cost
    if rested:
        out["r"] = 1
    if don:
        out["n"] = don
    if k := _keywords(kws):
        out["k"] = k
    return out


def _hand(defs: dict[str, CardDef], row: list) -> dict:
    uid, cid, cost, counter, revealed = row
    d = defs[cid]
    out = {"u": uid, "i": cid}
    if revealed:
        out["rv"] = 1
    out["c"] = cost
    if d.category == Category.CHARACTER:
        out["p"] = d.power
        out["x"] = counter
    return out


def _side(defs: dict[str, CardDef], ps: dict) -> dict:
    don_deck, don_active, don_rested = ps["don"]
    return {"L": _field(defs, ps["leader"]), "C": [_field(defs, r) for r in ps["chars"]],
            "S": _field(defs, ps["stage"]) if ps["stage"] else None, "H": [_hand(defs, r) for r in ps["hand"]],
            "life": [cid if up else None for cid, up in ps["life"]],
            "trash": ps["trash"], "deck": len(ps["deck"]), "don": [don_active, don_rested, don_deck]}


def _frame(g, defs: dict[str, CardDef], seen: int) -> dict:
    """The board of a Rust game (both hands) with the log lines after the first `seen`."""
    s = json.loads(g.snapshot_json())
    f = {"t": s["turn"], "ph": s["phase"], "tp": s["tp"], "log": g.log[seen:],
         "s": [_side(defs, s["players"][0]), _side(defs, s["players"][1])]}
    if s["battle"]:
        f["b"] = s["battle"]
    return f


def _def(d: CardDef) -> dict:
    from .env import rust_db
    return {"n": d.name, "cat": d.category.value, "col": list(d.colors), "cost": d.cost, "pow": d.power,
            "cnt": d.counter, "life": d.life, "ty": list(d.types), "txt": d.text, "trg": d.trigger_text,
            "un": rust_db().unimplemented(d.id)}


def record(decks: tuple[Deck, Deck], agents: tuple[Agent, Agent], seed: Optional[int] = None,
           names: tuple[str, str] = ("", "")) -> dict:
    """Play a game on the Rust engine, keeping a frame at every decision."""
    g = new_game(decks, seed)
    defs = card_defs(decks)
    frames, seen = [], 0
    for _ in range(MAX_STEPS):
        if g.over:
            break
        d = pending(g)
        f = _frame(g, defs, seen)
        seen = g.log_len
        k = agents[d["player"]].pick(g)
        _, card, target, _, _ = d["options"][k]
        f["d"] = {"pl": d["player"], "kind": d["kind"], "q": d["prompt"], "o": [o[4] for o in d["options"]],
                  "pick": k, "hl": [u for u in (card, target) if u is not None]}
        frames.append(f)
        g.step(k)
    else:
        raise RuntimeError(f"game did not finish in {MAX_STEPS} steps")
    frames.append(_frame(g, defs, seen))
    return {"players": [{"leader": decks[p].leader.name, "id": decks[p].leader.id, "agent": names[p]} for p in (0, 1)],
            "seed": seed, "winner": g.winner, "turns": g.turn_number, "first": first_player(frames),
            "cards": {i: _def(defs[i]) for i in sorted(defs)}, "frames": frames}


def to_html(replay: dict, out_dir: Optional[Path] = None, assets: Path = ASSETS) -> str:
    """The viewer page with the replay inside. Pass the folder the page will be saved in so it can
    find the card art; without it the page uses text cards."""
    replay = dict(replay)
    if out_dir is not None and (a := art(replay, assets)):
        a["base"] = Path(os.path.relpath(assets, Path(out_dir).resolve())).as_posix()
        replay["art"] = a
    return page(replay)


def page(data: dict) -> str:
    """The viewer with `data` inside it."""
    blob = json.dumps(data, separators=(",", ":")).replace("</", "<\\/")
    return VIEWER.read_text().replace("/*REPLAY_DATA*/null", blob)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deck0", default="rocks", help="default: your list in decks/rocks.txt")
    ap.add_argument("--deck1", default="(Yellow) Charlotte Linlin")
    ap.add_argument("--p0", default="greedy", help="random, greedy, a .pt checkpoint or mcts:<checkpoint>")
    ap.add_argument("--p1", default="greedy", help="random, greedy, a .pt checkpoint or mcts:<checkpoint>")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", help="default: replays/<seed>.html")
    ap.add_argument("--open", action="store_true", help="open the replay in your browser")
    args = ap.parse_args(argv)

    decks = (resolve_deck(args.deck0), resolve_deck(args.deck1))
    rep = record(decks, (make_agent(args.p0), make_agent(args.p1)), args.seed, names=(args.p0, args.p1))
    out = Path(args.out or f"replays/{args.seed}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_html(rep, out.parent))
    result = "draw" if rep["winner"] is None else f"player {rep['winner']} ({rep['players'][rep['winner']]['leader']}) wins"
    print(f"{len(rep['frames'])} frames, {rep['turns']} turns, {result}: {out}")
    if args.open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
