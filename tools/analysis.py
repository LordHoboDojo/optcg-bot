"""Compare the bots: every pair plays a set of games, plus each training run's progress, for the
site's Analysis page.

    .venv/bin/python tools/analysis.py --games 200            # writes data/analysis.json
    .venv/bin/python tools/analysis.py --games 20 --out /tmp/a.json

Networks play their best-scored move, on the Rust engine. Each pair alternates seats over the
Rocks vs Linlin matchup, so both bots play both decks equally often.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from optcg.nn.model import load  # noqa: E402
from optcg.nn.train import _pool, evaluate  # noqa: E402

MATCHUP = ("rocks", "(Yellow) Charlotte Linlin")

# name, spec (an agent or a checkpoint), what it is
BOTS = [
    ("random", "random", "Picks any legal option."),
    ("greedy", "greedy", "Hand-written rules: plays the biggest thing it can and attacks when it's ahead."),
    ("imitate", "runs/imitate.pt", "Network trained to copy greedy's moves."),
    ("ppo", "runs/ppo/latest.pt", "Imitation, then reinforcement learning (PPO) against greedy."),
    ("league", "runs/league/latest.pt", "PPO over every fully implemented deck pairing."),
    ("rocks", "runs/rocks/latest.pt", "PPO on your Rocks list against its own snapshots."),
    ("selfplay", "runs/selfplay/best.pt", "Trained on search (MCTS) visit counts and results."),
]

RUNS = [  # training logs: name, log file, what it trained
    ("ppo", "runs/train.log", "Imitation then PPO against greedy"),
    ("league", "runs/league.log", "PPO over the deck league"),
    ("rocks", "runs/rocks.log", "PPO on Rocks with snapshot opponents"),
    ("selfplay", "runs/selfplay.log", "Expert iteration from search"),
]

ITER = re.compile(r"^iter (\d+):.*?vs greedy (\d+)% \((\d+)\)(?:, vs snapshot (\d+)% \((\d+)\))?")
EVAL = re.compile(r"^\s+eval vs greedy \(.*?(\d+) games\): wins ([\d.]+)%")


def curves(log: Path) -> dict:
    """Win rate against greedy while training (sampled moves) and at each evaluation (best move)."""
    out = {"train": [], "eval": []}
    it = 0
    for line in log.read_text().splitlines():
        if m := ITER.match(line):
            it = int(m.group(1))
            out["train"].append([it, int(m.group(2)) / 100])
        elif (m := EVAL.match(line)) and it:
            out["eval"].append([it, float(m.group(2)) / 100, int(m.group(1))])
    return out


def ratings(names: list[str], results: dict, anchor: str = "greedy", base: float = 1500) -> dict:
    """Elo-style ratings fitted to every pair's score (Bradley-Terry, by minorization-maximization),
    with `anchor` at `base`. Draws count half."""
    won = {n: 0.0 for n in names}
    for (a, b), res in results.items():
        won[a] += res["score"] * res["games"]
        won[b] += (1 - res["score"]) * res["games"]
    strength = {n: 1.0 for n in names}
    for _ in range(500):
        new = {}
        for n in names:
            denom = sum(res["games"] / (strength[a] + strength[b]) for (a, b), res in results.items() if n in (a, b))
            new[n] = max(won[n], 0.5) / denom if denom else 1.0  # 0.5: a bot that never wins stays finite
        strength = new
    return {n: round(base + 400 * math.log10(strength[n] / strength[anchor])) for n in names}


def interval(p: float, n: int) -> list[float]:
    """95% Wilson interval for a win share."""
    if n == 0:
        return [0.0, 1.0]
    z = 1.96
    c = (p + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(max(0.0, c - h), 4), round(min(1.0, c + h), 4)]


def _sha(spec: str):
    """A checkpoint's hash, so a copy of it (e.g. the server's models/house.pt) is recognised."""
    import hashlib
    return hashlib.sha256((ROOT / spec).read_bytes()).hexdigest() if spec.endswith(".pt") else None


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--games", type=int, default=200, help="games per pair (even: seats alternate)")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--out", type=Path, default=ROOT / "data" / "analysis.json")
    args = ap.parse_args(argv)

    bots = [(n, s, d) for n, s, d in BOTS if s in ("random", "greedy") or (ROOT / s).is_file()]
    specs = {n: s if s in ("random", "greedy") else load(str(ROOT / s)).state_dict() for n, s, _ in bots}
    names = [n for n, _, _ in bots]
    results, started = {}, time.time()
    with _pool(args.workers) as pool:
        for a, b in itertools.combinations(names, 2):
            if isinstance(specs[a], str) and not isinstance(specs[b], str):
                a, b = b, a  # the batched path wants the network first
            t = time.time()
            win, draw, by_deck = evaluate(pool, args.workers, (MATCHUP, MATCHUP[::-1]), specs[a], specs[b], 128, args.games,
                                          engine="rust", parallel=16)
            score = win + draw / 2
            results[(a, b)] = {"score": score, "games": args.games}
            print(f"{a:>9} vs {b:<9} {win:6.1%} wins, {draw:5.1%} draws  ({time.time() - t:.0f}s)", flush=True)
            results[(a, b)].update(win=round(win, 4), draw=round(draw, 4), ci=interval(win, args.games),
                                   by_deck={deck: round(w, 4) for deck, (w, _) in by_deck.items()})

    elo = ratings(names, results)
    out = {
        "generated": time.strftime("%Y-%m-%d"),
        "matchup": list(MATCHUP),
        "games_per_pair": args.games,
        "seconds": round(time.time() - started),
        "bots": [{"name": n, "spec": s, "about": d, "rating": elo[n], "sha256": _sha(s)} for n, s, d in bots],
        "pairs": [{"a": a, "b": b, **{k: v for k, v in res.items() if k != "score"}} for (a, b), res in results.items()],
        "runs": [{"name": n, "about": d, **curves(ROOT / log)} for n, log, d in RUNS if (ROOT / log).is_file()],
    }
    args.out.write_text(json.dumps(out, indent=1))
    print(f"ratings: {', '.join(f'{n} {r}' for n, r in sorted(elo.items(), key=lambda x: -x[1]))}")
    print(f"-> {args.out} in {out['seconds']}s")


if __name__ == "__main__":
    main()
