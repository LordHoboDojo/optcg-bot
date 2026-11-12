"""League bookkeeping for multi-deck training (train.py --decks): which matchups and which
opponents the next games use, and the matchup table evaluations report.

A matchup is an ordered pair (my deck, their deck): the learner plays the first. With a deck list
every ordered pair is one, mirrors included, so 15 decks give 225.

MatchupSampler  prioritises the matchups where the learner's edge over a reference is smallest.
                The references are GreedyAgent and the current best checkpoint. Edge over greedy
                is the learner's win rate with my deck against greedy with theirs, minus greedy's
                own win rate in that matchup (greedy vs greedy, the `baseline`), so a matchup that
                is simply hard for my deck doesn't look like a weakness of the bot. Edge over the
                best is wins(A vs best B) + wins(B vs best A) - 1, zero when the learner plays
                like the best; it needs no baseline games. A matchup's priority is a softmax
                of -edge / temp, mixed with a uniform `floor` so every matchup keeps being played.
PFSP            AlphaStar's prioritised fictitious self-play: snapshot opponents are weighted by
                (1 - P[learner beats them]) ** power, so the ones the learner still loses to
                come up most.

Win rates count a draw as half a win. Both trackers decay old results (`decay` per iteration)
because the learner keeps changing, and start from a weak prior at the baseline (sampler) or
1/2 (PFSP), so a matchup or opponent with no games yet is treated as average.
"""
from __future__ import annotations

import itertools
import json
import math
import random
from pathlib import Path


def read_decks(path: str | Path) -> list[str]:
    """Deck names from a deck file: one per line; '#' starts a comment, blank lines are skipped."""
    names = [line.split("#", 1)[0].strip() for line in Path(path).read_text().splitlines()]
    names = [n for n in names if n]
    if len(names) < 2:
        raise ValueError(f"{path}: a league needs 2 or more decks, found {len(names)}")
    if len(set(names)) != len(names):
        raise ValueError(f"{path}: a deck is listed twice")
    return names


def ordered_matchups(decks: list[str], mirrors: bool = True) -> list[tuple[str, str]]:
    """Every (my deck, their deck); with `mirrors`, a deck against itself too."""
    return [(a, b) for a, b in itertools.product(decks, decks) if mirrors or a != b]


def short(deck: str) -> str:
    """'(Blue) Rocks.D.Xebec' -> 'Rocks.D.Xebec'."""
    return deck.split(") ", 1)[-1]


class _Rates:
    """Decaying win counts with a prior: rate = (wins + prior * mean) / (games + prior)."""

    def __init__(self, decay: float, prior: float):
        self.decay, self.prior = decay, prior
        self.counts: dict = {}

    def record(self, key, score: float, games: float = 1.0) -> None:
        w, n = self.counts.get(key, (0.0, 0.0))
        self.counts[key] = (w + score, n + games)

    def step(self) -> None:
        self.counts = {k: (w * self.decay, n * self.decay) for k, (w, n) in self.counts.items()}

    def games(self, key) -> float:
        return self.counts.get(key, (0.0, 0.0))[1]

    def rate(self, key, mean: float = 0.5) -> float:
        w, n = self.counts.get(key, (0.0, 0.0))
        return (w + self.prior * mean) / (n + self.prior)


class MatchupSampler:
    def __init__(self, matchups: list[tuple[str, str]], floor: float = 0.3, temp: float = 0.1,
                 decay: float = 0.95, prior: float = 4.0):
        if not 0 <= floor <= 1:
            raise ValueError("floor must be between 0 and 1")
        if temp <= 0:
            raise ValueError("temp must be positive")
        self.matchups = list(matchups)
        self.floor, self.temp = floor, temp
        self.refs = {"greedy": _Rates(decay, prior), "best": _Rates(decay, prior)}
        self.baseline: dict[tuple[str, str], float] = {}

    def set_baseline(self, rates: dict[tuple[str, str], float]) -> None:
        """Greedy's own win rate with my deck against greedy with theirs, per matchup."""
        self.baseline = dict(rates)

    def record(self, ref: str, matchup: tuple[str, str], score: float, games: float = 1.0) -> None:
        """A result of the learner playing matchup[0] against `ref` playing matchup[1]:
        `score` is wins (a draw counts 1/2) out of `games`. Other opponents are ignored."""
        if ref in self.refs:
            self.refs[ref].record(tuple(matchup), score, games)

    def reset(self, ref: str) -> None:
        """Forget the results against `ref`, e.g. when the best checkpoint changes."""
        self.refs[ref].counts.clear()

    def step(self) -> None:
        """Age every result once per iteration."""
        for r in self.refs.values():
            r.step()

    def edge(self, matchup: tuple[str, str]) -> float:
        """The learner's edge over the references in this matchup (the mean over those with games)."""
        a, b = matchup
        edges = []
        g = self.refs["greedy"]
        if g.games((a, b)) > 0:
            base = self.baseline.get((a, b), 0.5)
            edges.append(g.rate((a, b), base) - base)
        best = self.refs["best"]
        if best.games((a, b)) + best.games((b, a)) > 0:
            edges.append(best.rate((a, b)) + best.rate((b, a)) - 1)
        return sum(edges) / len(edges) if edges else 0.0

    def probs(self) -> list[float]:
        """Sampling probability of each matchup, in self.matchups order."""
        logits = [-self.edge(m) / self.temp for m in self.matchups]
        top = max(logits)
        w = [math.exp(x - top) for x in logits]
        total = sum(w)
        n = len(self.matchups)
        return [(1 - self.floor) * x / total + self.floor / n for x in w]

    def sample(self, rng: random.Random, k: int) -> list[tuple[str, str]]:
        return rng.choices(self.matchups, weights=self.probs(), k=k)


class PFSP:
    """Weights for the snapshot opponents, keyed by any hashable id."""

    def __init__(self, power: float = 2.0, decay: float = 0.95, prior: float = 4.0):
        self.power = power
        self.rates = _Rates(decay, prior)

    def record(self, opponent, score: float) -> None:
        """The learner scored `score` (1 win, 1/2 draw, 0 loss) in a game against `opponent`."""
        self.rates.record(opponent, score)

    def step(self) -> None:
        self.rates.step()

    def win_rate(self, opponent) -> float:
        """The learner's win rate against `opponent`."""
        return self.rates.rate(opponent)

    def weights(self, opponents: list) -> list[float]:
        """f_hard(x) = (1 - x) ** power with x the learner's win rate, normalised. An opponent the
        learner always beats gets a small weight floor, so it isn't dropped for good."""
        w = [max(1 - self.win_rate(o), 0.02) ** self.power for o in opponents]
        total = sum(w)
        return [x / total for x in w]


# ---------------------------------------------------------------- the matchup table

def table(decks: list[str], results: dict, baseline: dict | None = None, **meta) -> dict:
    """A JSON-ready matchup table. `results` and `baseline` map (my deck, their deck) to
    [score, games]. Rows are my deck, columns theirs; cells with no games are None."""
    def grid(res):
        return [[(res[(a, b)][0] / res[(a, b)][1]) if res.get((a, b), (0, 0))[1] else None for b in decks]
                for a in decks]

    def mean(rows):
        cells = [x for row in rows for x in row if x is not None]
        return sum(cells) / len(cells) if cells else None

    out = {"decks": decks, "win": grid(results), **meta}
    out["mean_win"] = mean(out["win"])
    if baseline is not None:
        out["baseline"] = grid(baseline)
        out["edge"] = [[None if w is None or g is None else w - g for w, g in zip(wr, gr)]
                       for wr, gr in zip(out["win"], out["baseline"])]
        out["mean_edge"] = mean(out["edge"])
    out["games"] = sum(n for _, n in results.values())
    return out


def format_table(t: dict, key: str = "win") -> str:
    """The table as text: rows are the learner's deck, columns the opponent's, in percent."""
    decks = t["decks"]
    names = [short(d)[:14] for d in decks]
    width = max(len(n) for n in names)
    lines = [" " * (width + 4) + "".join(f"{i + 1:>5}" for i in range(len(decks))) + "   mean"]
    for i, (name, row) in enumerate(zip(names, t[key])):
        cells = "".join("    ." if x is None else f"{100 * x:5.0f}" for x in row)
        valid = [x for x in row if x is not None]
        avg = f"  {100 * sum(valid) / len(valid):5.1f}" if valid else ""
        lines.append(f"{i + 1:>2} {name:<{width}} " + cells + avg)
    return "\n".join(lines)


def summary(t: dict) -> str:
    parts = [f"mean win rate vs greedy {t['mean_win']:.1%}"]
    if t.get("mean_edge") is not None:
        parts.append(f"mean edge over greedy {t['mean_edge']:+.1%}")
    return ", ".join(parts) + f" ({t['games']} games)"


def save_table(t: dict, path: str | Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(t, indent=1))
