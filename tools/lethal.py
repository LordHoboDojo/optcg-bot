"""How well a bot finds lethal: of the turns where it had a forced win, how many did it win?

A bot plays games against an opponent on the Rust engine. At the start of each of its turns
(its first Main Phase decision) with the opponent on few Life cards, an exhaustive search over
the rest of that turn (engine-rs/src/lethal.rs) asks whether the bot can win it whatever the opponent does: every choice
of the bot's is tried, and every choice of the opponent's (blocks, counters, Triggers...) must
lose for the opponent. The search sees everything, the opponent's hand and the deck order
included, so "forced" means against the cards actually there; a bot that holds back for fear of
a counter the opponent doesn't have is counted as missing lethal. Positions are shared between
move orders (giving DON!! to A then B is the same as B then A), and a search that runs past
--budget positions is reported as unknown.

    .venv/bin/python tools/lethal.py runs/selfplay/best.pt models/house.pt greedy --games 200
    .venv/bin/python tools/lethal.py runs/selfplay/best.pt --show 5    # print missed turns
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from optcg.env import rust_game  # noqa: E402
from optcg.play import make_agent, resolve_deck  # noqa: E402

DECKS = ("rocks", "(Yellow) Charlotte Linlin")  # the house bots' training decks


def life(g, player: int) -> int:
    return len(json.loads(g.snapshot_json())["players"][player]["life"])


@dataclass
class Turn:
    seed: int
    seat: int
    decks: tuple[str, str]
    turn: int
    opp_life: int
    forced: bool | None
    nodes: int
    seconds: float
    won: bool  # the bot won during this turn
    start: int = 0  # where the turn begins in the game's log
    log: list[str] = field(default_factory=list)


def play(job) -> list[Turn]:
    """One game: the bot in `seat`; every turn of the bot's with the opponent on <= max_life
    Life is checked."""
    bot_spec, vs_spec, seed, seat, names, max_life, budget = job
    import torch
    torch.set_num_threads(1)
    bot, opp = make_agent(bot_spec), make_agent(vs_spec)
    decks = tuple(resolve_deck(n) for n in names)
    g = rust_game(decks, seed, fast=False)
    agents = (bot, opp) if seat == 0 else (opp, bot)
    out: list[Turn] = []
    checked, open_turn = -1, None
    while not g.over:
        if open_turn and g.turn_number != open_turn.turn:
            open_turn = None
        p = g.pending_player()
        if p == seat and g.turn_number != checked and json.loads(g.pending_json())["kind"] == "main":
            checked = g.turn_number
            n = life(g, 1 - seat)
            if n <= max_life:
                t0 = time.perf_counter()
                forced, nodes = g.forced_win(seat, budget)  # engine-rs/src/lethal.rs
                open_turn = Turn(seed, seat, names, g.turn_number, n, forced, nodes,
                                 time.perf_counter() - t0, False, g.log_len)
                out.append(open_turn)
        g.step(agents[p].pick(g))
        if open_turn and g.over:
            open_turn.won = g.winner == seat
    for t in out:  # the bot's own turn, for printing misses
        if t.forced and not t.won:
            log = g.log
            t.log = log[t.start:t.start + 80]
    return out


def report(name: str, turns: list[Turn], show: int) -> dict:
    forced = [t for t in turns if t.forced]
    no = [t for t in turns if t.forced is False]
    unknown = [t for t in turns if t.forced is None]
    took = sum(t.won for t in forced)
    stole = sum(t.won for t in no)  # won anyway: the opponent defended badly
    by_life = {}
    for t in forced:
        a, b = by_life.get(t.opp_life, (0, 0))
        by_life[t.opp_life] = (a + t.won, b + 1)
    print(f"\n{name}: {len(turns)} turns checked, {len(forced)} with a forced win, {len(no)} without, "
          f"{len(unknown)} unknown (over budget)")
    if forced:
        print(f"  took lethal {took}/{len(forced)} = {took / len(forced):.1%}"
              + "".join(f" | opp on {k} life: {a}/{b}" for k, (a, b) in sorted(by_life.items())))
    if unknown:
        print(f"  won on {sum(t.won for t in unknown)}/{len(unknown)} of the turns the search couldn't settle")
    if no:
        print(f"  won anyway on {stole}/{len(no)} turns with no forced win (the opponent misdefended)")
    secs = sorted(t.seconds for t in turns)
    if secs:
        print(f"  search: median {secs[len(secs) // 2] * 1000:.0f} ms, max {secs[-1]:.1f} s, "
              f"max {max(t.nodes for t in turns)} positions")
    for t in [t for t in forced if not t.won][:show]:
        print(f"\n  missed: seed {t.seed}, seat {t.seat}, {t.decks[t.seat]} vs {t.decks[1 - t.seat]}, "
              f"turn {t.turn}, opponent on {t.opp_life} life")
        for line in t.log:
            print("    " + line)
    return {"turns": len(turns), "forced": len(forced), "took": took, "no_forced": len(no),
            "won_anyway": stole, "unknown": len(unknown), "won_unknown": sum(t.won for t in unknown)}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bots", nargs="+", help="bot specs: greedy, random, a .pt checkpoint, mcts:<checkpoint>")
    ap.add_argument("--vs", default="greedy", help="the opponent")
    ap.add_argument("--games", type=int, default=100, help="per bot; seats and deck pairings alternate")
    ap.add_argument("--max-life", type=int, default=2, help="check turns with the opponent on at most this many Life")
    ap.add_argument("--budget", type=int, default=20_000, help="positions per search before giving up")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--show", type=int, default=0, help="print this many missed turns per bot")
    ap.add_argument("--json", help="write the summary here")
    args = ap.parse_args()

    pairs = [(a, b) for a in DECKS for b in DECKS]
    summary = {}
    with mp.get_context("spawn").Pool(args.workers) as pool:
        for spec in args.bots:
            jobs = [(spec, args.vs, args.seed + k, k % 2, pairs[(k // 2) % len(pairs)], args.max_life, args.budget)
                    for k in range(args.games)]
            t0 = time.time()
            turns = [t for game in pool.imap_unordered(play, jobs) for t in game]
            summary[spec] = report(f"{spec} vs {args.vs} ({args.games} games, {time.time() - t0:.0f}s)", turns,
                                   args.show)
    if args.json:
        Path(args.json).write_text(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
