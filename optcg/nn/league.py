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
MetaGame        head-to-head results between league members (main-agent checkpoints, exploiter
                copies, greedy), ranked by the Nash equilibrium of that meta-game as in PSRO.

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
    names = [line.split("#", 1)[0].strip() for line in Path(path).read_text(encoding="utf-8").splitlines()]
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
    Path(path).write_text(json.dumps(t, indent=1), encoding="utf-8")


# ---------------------------------------------------------------- the Nash meta-game (PSRO-style ranking)

def nash(payoff: list[list[float]], iters: int = 5000) -> list[float]:
    """A Nash equilibrium of the symmetric zero-sum game where payoff[i][j] is i's expected result
    against j, centred so that payoff[j][i] == -payoff[i][j]. Regret matching+ with linearly
    weighted averaging (its average strategy converges to an equilibrium; both players share it
    since the game is symmetric)."""
    import numpy as np
    a = np.asarray(payoff, dtype=float)
    n = len(a)
    if n == 1:
        return [1.0]
    regret, avg = np.zeros(n), np.zeros(n)
    for t in range(1, iters + 1):
        pos = np.maximum(regret, 0.0)
        x = pos / pos.sum() if pos.sum() > 0 else np.full(n, 1.0 / n)
        u = a @ x
        regret = np.maximum(regret + u - x @ u, 0.0)
        avg += t * x
    p = avg / avg.sum()
    p[p < 1e-4] = 0.0
    return [float(x) for x in p / p.sum()]


class MetaGame:
    """Head-to-head results between league members (main-agent checkpoints, exploiter copies,
    greedy) and their ranking by the Nash meta-game, as in PSRO and Nash averaging: a member's
    rating is its expected result against the Nash mixture of the whole population, so a
    checkpoint that only beats easy opponents, or is beaten by an exploiter, doesn't come out on
    top just by farming them. A rating of 0 means nobody in the population does better against
    the mixture. Win rates count draws as half and start from a prior of one win in two games."""

    def __init__(self):
        self.members: list[str] = []
        self.kind: dict[str, str] = {}
        self.results: dict[tuple[str, str], list[float]] = {}

    def add(self, name: str, kind: str) -> None:
        if name in self.kind:
            raise ValueError(f"{name} is already a member")
        self.members.append(name)
        self.kind[name] = kind

    def remove(self, name: str) -> None:
        self.members.remove(name)
        del self.kind[name]
        self.results = {k: v for k, v in self.results.items() if name not in k}

    def record(self, a: str, b: str, score: float, games: float) -> None:
        """`a` scored `score` (wins, a draw counting 1/2) in `games` games against `b`."""
        for k, s in (((a, b), score), ((b, a), games - score)):
            r = self.results.setdefault(k, [0.0, 0.0])
            r[0] += s
            r[1] += games

    def games(self, a: str, b: str) -> float:
        return self.results.get((a, b), (0.0, 0.0))[1]

    def rate(self, a: str, b: str) -> float:
        w, n = self.results.get((a, b), (0.0, 0.0))
        return (w + 1) / (n + 2)

    def payoff(self) -> list[list[float]]:
        return [[0.0 if a == b else self.rate(a, b) - 0.5 for b in self.members] for a in self.members]

    def solve(self) -> dict[str, tuple[float, float]]:
        """{member: (its weight in the Nash mixture, its expected result against the mixture)}."""
        a = self.payoff()
        p = nash(a)
        return {m: (p[i], sum(x * w for x, w in zip(a[i], p))) for i, m in enumerate(self.members)}

    def ranking(self, kind: str | None = None) -> list[str]:
        """Members (of `kind`, if given) from best to worst: by result against the Nash mixture,
        rounded to 1%, then by weight in it."""
        sol = self.solve()
        names = [m for m in self.members if kind is None or self.kind[m] == kind]
        return sorted(names, key=lambda m: (round(sol[m][1], 2), sol[m][0]), reverse=True)

    def prune(self, limit: int, keep: set[str]) -> list[str]:
        """Drop members, never those in `keep`, until at most `limit` are left: those with the
        least weight in the Nash mixture first, the oldest among equals. Returns the dropped."""
        dropped = []
        while len(self.members) > limit:
            sol = self.solve()
            order = [m for m in self.members if m not in keep]
            if not order:
                break
            m = min(order, key=lambda x: (sol[x][0], self.members.index(x)))
            self.remove(m)
            dropped.append(m)
        return dropped

    def to_json(self) -> dict:
        return {"members": [[m, self.kind[m]] for m in self.members],
                "results": [[a, b, w, n] for (a, b), (w, n) in self.results.items()]}

    @classmethod
    def from_json(cls, d: dict) -> "MetaGame":
        g = cls()
        for m, kind in d["members"]:
            g.add(m, kind)
        g.results = {(a, b): [w, n] for a, b, w, n in d["results"]}
        return g

    def format(self, top: int = 8) -> str:
        sol = self.solve()
        rows = sorted(self.members, key=lambda m: (round(sol[m][1], 2), sol[m][0]), reverse=True)[:top]
        return ", ".join(f"{m} {sol[m][1]:+.1%} (Nash {sol[m][0]:.2f})" for m in rows)


def weakest(t: dict, k: int, key: str = "edge") -> list[tuple[str, str]]:
    """The `k` matchups of a matchup table with the lowest `key` (edge over greedy by default):
    where a main exploiter should look for holes."""
    decks = t["decks"]
    cells = [(t[key][i][j], a, b) for i, a in enumerate(decks) for j, b in enumerate(decks)
             if t[key][i][j] is not None]
    return [(a, b) for _, a, b in sorted(cells)[:k]]
