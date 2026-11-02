"""Play the same game in the Python and Rust engines and compare them after every decision."""
import json
import random

import optcg_rs
from optcg import Game
from optcg.agents import GreedyAgent
from optcg.cards import (CANNOT_ATTACK, KEYWORDS, NO_KO_BY_EFFECTS, NO_KO_BY_OPP_EFFECTS, NO_KO_IN_BATTLE)
from optcg.carddb import coverage, official_decks

ALL_KW = sorted(KEYWORDS | {CANNOT_ATTACK, NO_KO_IN_BATTLE, NO_KO_BY_EFFECTS, NO_KO_BY_OPP_EFFECTS})
_db = None


def rust_db():
    global _db
    if _db is None:
        _db = optcg_rs.Db("data/cards.json")
    return _db


def covered():
    ds = official_decks()
    return {n: d for n, d in ds.items() if coverage(d)[0] == coverage(d)[1]}


def field_card(g, c):
    return [c.uid, c.d.id, g.power(c), g.cost(c), c.rested, c.don, sorted(k for k in ALL_KW if g.has_keyword(c, k))]


def snapshot(g):
    def side(p):
        ps = g.players[p]
        return {"leader": field_card(g, ps.leader), "chars": [field_card(g, c) for c in ps.characters],
                "stage": field_card(g, ps.stage) if ps.stage else None,
                "hand": [[c.uid, c.d.id, g.cost(c), g.counter_value(c)] for c in ps.hand],
                "deck": [c.d.id for c in ps.deck], "trash": [c.d.id for c in ps.trash],
                "life": [[c.d.id, c.face_up] for c in ps.life],
                "don": [ps.don_deck, ps.don_active, ps.don_rested]}
    return {"turn": g.turn_number, "tp": g.turn_player, "phase": g.phase,
            "battle": [g.battle.attacker.uid, g.battle.target.uid] if g.battle else None,
            "players": [side(0), side(1)]}


def pending(g):
    d = g.pending
    if d is None:
        return None
    return {"player": d.player, "kind": d.kind, "prompt": d.prompt,
            "options": [[o.kind, o.card, o.target, o.index, str(o)] for o in d.options]}


class Mismatch(AssertionError):
    pass


def play_both(decks, seed, policy, rng):
    """Returns the number of decisions. Raises Mismatch with details on the first difference."""
    py = Game(decks, seed=seed)
    rs = optcg_rs.Game(rust_db(), [(d.leader.id, [c.id for c in d.cards]) for d in decks], seed)
    agents = (GreedyAgent(), GreedyAgent())
    n = 0
    while True:
        a, b = snapshot(py), json.loads(rs.snapshot_json())
        if a != b:
            raise Mismatch(_diff("state", a, b, py, n))
        a, b = pending(py), (json.loads(rs.pending_json()) if rs.pending_json() else None)
        if a != b:
            raise Mismatch(_diff("decision", a, b, py, n))
        if len(py.log) != rs.log_len:
            raise Mismatch(_diff("log length", len(py.log), rs.log_len, py, n) + f"\n  rs last: {rs.log[-3:]}")
        if py.over:
            break
        if policy == "greedy":
            k = py.pending.options.index(agents[py.pending.player].act(py, py.pending))
        else:
            k = rng.randrange(len(py.pending.options))
        py.step(py.pending.options[k])
        rs.step(k)
        n += 1
    if py.log != rs.log or (py.winner, py.over) != (rs.winner, rs.over):
        for i, (x, y) in enumerate(zip(py.log, rs.log)):
            if x != y:
                raise Mismatch(f"log line {i}:\n  py: {x}\n  rs: {y}")
        raise Mismatch(f"log lengths {len(py.log)} vs {len(rs.log)}, winners {py.winner} vs {rs.winner}")
    return n


def _diff(what, a, b, py, n):
    lines = [f"{what} differs after {n} decisions (turn {py.turn_number}):"]
    def walk(x, y, path):
        if type(x) != type(y) or not isinstance(x, (dict, list)):
            if x != y:
                lines.append(f"  {path}: py={x!r} rs={y!r}")
            return
        if isinstance(x, dict):
            for k in sorted(set(x) | set(y)):
                walk(x.get(k), y.get(k), f"{path}.{k}")
        else:
            if len(x) != len(y):
                lines.append(f"  {path}: lengths {len(x)} vs {len(y)}")
            for i, (p, q) in enumerate(zip(x, y)):
                walk(p, q, f"{path}[{i}]")
    walk(a, b, "")
    lines += ["  last log lines:"] + [f"    {l}" for l in py.log[-6:]]
    return "\n".join(lines[:40])


if __name__ == "__main__":
    import itertools, sys
    games = int(sys.argv[1]) if len(sys.argv) > 1 else 20
    ds = covered()
    rng = random.Random(0)
    total = 0
    for (n0, n1) in itertools.product(sorted(ds), repeat=2):
        for policy in ("random", "greedy"):
            for seed in range(games):
                try:
                    total += play_both((ds[n0], ds[n1]), seed, policy, rng)
                except Mismatch as e:
                    print(f"MISMATCH {n0} vs {n1}, seed {seed}, {policy}\n{e}")
                    sys.exit(1)
    print(f"all games match: {total} decisions")
