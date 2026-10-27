"""How fast the engine plays, which sets how long training takes.

    python3 tools/bench.py                     # sample decks
    python3 tools/bench.py --deck0 "(Blue) Rocks.D.Xebec" --deck1 "(Yellow) Charlotte Linlin"
"""
from __future__ import annotations

import argparse
import random
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from optcg.agents import GreedyAgent, RandomAgent  # noqa: E402
from optcg.env import Env  # noqa: E402
from optcg.play import play_game, resolve_deck  # noqa: E402


def timed(label: str, games: int, run) -> None:
    start = time.perf_counter()
    decisions = sum(run(i) for i in range(games))
    secs = time.perf_counter() - start
    print(f"{label:<40} {games / secs:7.1f} games/s  {decisions / secs:8.0f} decisions/s  "
          f"({decisions / games:.0f} decisions/game)")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--deck0", default="sample-red")
    ap.add_argument("--deck1", default="sample-green")
    ap.add_argument("--games", type=int, default=200)
    args = ap.parse_args()
    decks = (resolve_deck(args.deck0), resolve_deck(args.deck1))

    timed("random vs random", args.games,
          lambda i: len(play_game(decks, (RandomAgent(i), RandomAgent(i + 1)), seed=i).history))
    timed("greedy vs greedy", args.games,
          lambda i: len(play_game(decks, (GreedyAgent(), GreedyAgent()), seed=i).history))

    def env_game(i):
        env, rng = Env(*decks, opponent=GreedyAgent()), random.Random(i)
        obs = env.reset(i)
        while not env.done:
            obs, _, _ = env.step(rng.randrange(len(obs["options"])))
        return env.steps
    timed("env: random learner + encode vs greedy", args.games, env_game)


if __name__ == "__main__":
    main()
