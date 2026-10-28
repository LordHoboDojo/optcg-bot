"""Record a game and write it as a self-contained HTML slideshow.

    python3 -m optcg.replay --deck0 "(Blue) Rocks.D.Xebec" --deck1 "(Yellow) Charlotte Linlin" --p1 greedy --open
    .venv/bin/python -m optcg.replay --p0 runs/ppo/latest.pt --p1 greedy --seed 7 --out replays/net_vs_greedy.html

One frame per decision: the board when the decision was asked (both hands visible), the options,
the one chosen, and the log lines since the previous frame. A last frame shows the end of the game.
"""
from __future__ import annotations

import argparse
import json
import webbrowser
from pathlib import Path
from typing import Optional

from .agents import Agent
from .cards import CANNOT_ATTACK, KEYWORDS, Card, Category
from .deck import Deck
from .engine import Game
from .play import MAX_STEPS, make_agent, resolve_deck

VIEWER = Path(__file__).with_name("replay_viewer.html")


def _card(g: Game, c: Card) -> dict:
    out = {"u": c.uid, "i": c.d.id}
    if c.zone in ("leader", "character", "stage"):
        if c.category in (Category.LEADER, Category.CHARACTER):
            out["p"] = g.power(c)
        if c.category != Category.LEADER:
            out["c"] = g.cost(c)
        if c.rested:
            out["r"] = 1
        if c.don:
            out["n"] = c.don
        kws = [k for k in sorted(KEYWORDS) if g.has_keyword(c, k)]
        if g.has_keyword(c, CANNOT_ATTACK):
            kws.append("Can't attack")
        if kws:
            out["k"] = kws
    elif c.zone == "hand":
        out["c"] = g.cost(c)
        if c.category == Category.CHARACTER:
            out["p"] = c.d.power
            out["x"] = g.counter_value(c)
    return out


def _side(g: Game, p: int) -> dict:
    ps = g.players[p]
    return {"L": _card(g, ps.leader), "C": [_card(g, c) for c in ps.characters],
            "S": _card(g, ps.stage) if ps.stage else None, "H": [_card(g, c) for c in ps.hand],
            "life": [c.d.id if c.face_up else None for c in ps.life],
            "trash": [c.d.id for c in ps.trash], "deck": len(ps.deck),
            "don": [ps.don_active, ps.don_rested, ps.don_deck]}


def _frame(g: Game, seen: int) -> dict:
    f = {"t": g.turn_number, "ph": g.phase, "tp": g.turn_player, "log": g.log[seen:],
         "s": [_side(g, 0), _side(g, 1)]}
    if g.battle:
        f["b"] = [g.battle.attacker.uid, g.battle.target.uid]
    return f


def _def(g: Game, card_id: str) -> dict:
    d = next(c.d for c in g.cards.values() if c.d.id == card_id)
    return {"n": d.name, "cat": d.category.value, "col": list(d.colors), "cost": d.cost, "pow": d.power,
            "cnt": d.counter, "life": d.life, "ty": list(d.types), "txt": d.text, "trg": d.trigger_text,
            "un": list(d.unimplemented)}


def record(decks: tuple[Deck, Deck], agents: tuple[Agent, Agent], seed: Optional[int] = None,
           names: tuple[str, str] = ("", "")) -> dict:
    """Play a game, keeping a frame at every decision."""
    g = Game(decks, seed=seed)
    frames, seen = [], 0
    for _ in range(MAX_STEPS):
        if g.over:
            break
        d = g.pending
        f = _frame(g, seen)
        seen = len(g.log)
        a = agents[d.player].act(g, d)
        f["d"] = {"pl": d.player, "kind": d.kind, "q": d.prompt, "o": [str(o) for o in d.options],
                  "pick": d.options.index(a), "hl": [u for u in (a.card, a.target) if u is not None]}
        frames.append(f)
        g.step(a)
    else:
        raise RuntimeError(f"game did not finish in {MAX_STEPS} steps")
    frames.append(_frame(g, seen))
    ids = {c.d.id for c in g.cards.values()}
    return {"players": [{"leader": decks[p].leader.name, "agent": names[p]} for p in (0, 1)],
            "seed": seed, "winner": g.winner, "turns": g.turn_number, "first": g.first_player,
            "cards": {i: _def(g, i) for i in sorted(ids)}, "frames": frames}


def to_html(replay: dict) -> str:
    data = json.dumps(replay, separators=(",", ":")).replace("</", "<\\/")
    return VIEWER.read_text().replace("/*REPLAY_DATA*/null", data)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deck0", default="(Blue) Rocks.D.Xebec")
    ap.add_argument("--deck1", default="(Yellow) Charlotte Linlin")
    ap.add_argument("--p0", default="greedy", help="random, greedy or a .pt checkpoint")
    ap.add_argument("--p1", default="greedy", help="random, greedy or a .pt checkpoint")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", help="default: replays/<seed>.html")
    ap.add_argument("--open", action="store_true", help="open the replay in your browser")
    args = ap.parse_args(argv)

    decks = (resolve_deck(args.deck0), resolve_deck(args.deck1))
    rep = record(decks, (make_agent(args.p0), make_agent(args.p1)), args.seed, names=(args.p0, args.p1))
    out = Path(args.out or f"replays/{args.seed}.html")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(to_html(rep))
    result = "draw" if rep["winner"] is None else f"player {rep['winner']} ({rep['players'][rep['winner']]['leader']}) wins"
    print(f"{len(rep['frames'])} frames, {rep['turns']} turns, {result}: {out}")
    if args.open:
        webbrowser.open(out.resolve().as_uri())


if __name__ == "__main__":
    main()
