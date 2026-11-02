"""Search on top of the policy network: information-set Monte Carlo tree search (ISMCTS).

At each decision the bot imagines the cards it can't see (a determinization: the opponent's
hand, both decks and face-down Life reshuffled, consistent with everything it has seen) and
plays the game forward in many such worlds on the Rust engine. All worlds share one tree, keyed
by what each option means rather than its place in the list (Game.option_keys), so the
opponent's moves with cards it might hold are searched too (single-observer ISMCTS).

The network guides the search the way AlphaZero's does: its move scores are each option's prior
(PUCT) and its value head scores the position where a simulation stops, so there are no random
playouts. One network call costs ~0.6 ms and a batch of 8 ~1.5 ms, so leaves are scored in
batches, with virtual loss keeping a batch's simulations apart.

The network is the same checkpoint the plain bot plays; only the move choice differs, so the
two compare directly:

    .venv/bin/python -m optcg.search runs/rocks/latest.pt --vs net --games 200
    .venv/bin/python -m optcg.search runs/rocks/latest.pt --vs greedy --sims 400

It needs the Rust engine (engine-rs/).
"""
from __future__ import annotations

import argparse
import math
import multiprocessing as mp
import os
import random
import time
from collections import Counter
from typing import Optional

import torch

from .agents import AGENTS, Agent
from .env import encode_rust, play_rust
from .nn.model import NeuralAgent, PolicyNet, collate, load


class Node:
    """One decision in the shared tree. Per option key: prior, visits, total value for the
    player choosing here, and how often the option was available (worlds differ in what the
    opponent holds, so some options exist only in some of them)."""
    __slots__ = ("prior", "visits", "value", "avail", "children")

    def __init__(self):
        self.prior: dict = {}
        self.visits: dict = {}
        self.value: dict = {}
        self.avail: dict = {}
        self.children: dict = {}


class MCTSAgent(Agent):
    """Plays the option the search visits most. `sims` simulations per decision, spread over
    up to `worlds` determinizations; `batch` leaves per network call; `c_puct` weighs the
    network's prior against the results so far. `noise` mixes Dirichlet noise into the root's
    priors, as AlphaZero does in its training games, so the search tries moves the network
    would overlook."""
    rust_only = True

    def __init__(self, net: PolicyNet, sims: int = 200, worlds: int = 32, batch: int = 8, c_puct: float = 1.5,
                 seed: Optional[int] = None, noise: float = 0.0, alpha: float = 0.3):
        self.net, self.sims, self.worlds, self.batch, self.c_puct = net.eval(), sims, worlds, batch, c_puct
        self.noise, self.alpha = noise, alpha
        self.rng = random.Random(seed)

    def act(self, game, decision):
        raise NotImplementedError("MCTSAgent searches on the Rust engine: give it an optcg_rs.Game via pick()")

    def pick(self, game) -> int:
        if game.num_options() == 1:
            return 0
        visits, prior = self.policy(game)
        return max(range(len(visits)), key=lambda i: (visits[i], prior[i]))

    @torch.no_grad()
    def policy(self, game) -> tuple[list[float], list[float]]:
        """Search the pending decision. Returns each option's share of the root's visits, and
        the network's prior for it (options with one key split it). Without a world that fits
        what the player has seen, both are the network's own move scores."""
        me = game.pending_player()
        worlds = self._worlds(game, me)
        if not worlds:
            logits, _ = self.net(collate([encode_rust(game)]))
            p = torch.softmax(logits[0], -1).tolist()
            return p, p
        root = Node()
        done = 0
        while done < self.sims:
            k = 1 if done == 0 else min(self.batch, self.sims - done)  # the root first, on its own
            leaves = [self._descend(root, worlds[(done + i) % len(worlds)].fork(), me) for i in range(k)]
            self._score(leaves, me)
            if done == 0 and self.noise:
                self._add_noise(root)
            done += k
        keys = game.option_keys(me)
        share = Counter(keys)
        prior = [root.prior.get(k, 0.0) / share[k] for k in keys]
        visits = [root.visits.get(k, 0) / share[k] for k in keys]
        total = sum(visits)
        return ([v / total for v in visits] if total else prior), prior

    def _add_noise(self, root: Node) -> None:
        keys = list(root.prior)
        d = [self.rng.gammavariate(self.alpha, 1.0) for _ in keys]
        total = sum(d) or 1.0
        for k, x in zip(keys, d):
            root.prior[k] = (1 - self.noise) * root.prior[k] + self.noise * x / total

    def _worlds(self, game, me: int) -> list:
        out = []
        for _ in range(4 * self.worlds):
            w = game.fork()
            if w.determinize(me, self.rng.randrange(1 << 62)):
                out.append(w)
                if len(out) == self.worlds:
                    break
        return out

    def _descend(self, node: Node, g, me: int):
        """Walk down from the root in world `g`, adding virtual loss, to a position the
        network must score (a new decision, or options this node hasn't seen) or the end of
        the game. Returns (path, node reached, game, terminal value for `me` or None)."""
        path = []
        while True:
            if g.over:
                return path, node, g, 0.0 if g.winner is None else (1.0 if g.winner == me else -1.0)
            if g.num_options() == 1:
                g.step(0)
                continue
            keys = g.option_keys(me)
            if any(k not in node.prior for k in keys):
                return path, node, g, None
            options = set(keys)
            c = self.c_puct * math.sqrt(sum(node.avail.get(k, 0) for k in options) + 1)
            key = max(options, key=lambda k: node.value.get(k, 0.0) / max(1, node.visits.get(k, 0))
                      + c * node.prior[k] / (1 + node.visits.get(k, 0)))
            for k in options:
                node.avail[k] = node.avail.get(k, 0) + 1
            node.visits[key] = node.visits.get(key, 0) + 1  # virtual loss until the leaf is scored
            node.value[key] = node.value.get(key, 0.0) - 1.0
            path.append((node, key, g.pending_player() == me))
            g.step(keys.index(key))
            node = node.children.setdefault(key, Node())

    def _score(self, leaves, me: int) -> None:
        """Score the leaves needing the network in one call, give their nodes priors for the
        options they hadn't seen, and back every result up its path."""
        values = [leaf[3] for leaf in leaves]
        todo = [i for i, leaf in enumerate(leaves) if leaf[3] is None]
        if todo:
            logits, value = self.net(collate([encode_rust(leaves[i][2]) for i in todo]))
            for j, i in enumerate(todo):
                _, node, g, _ = leaves[i]
                v = max(-1.0, min(1.0, float(value[j])))  # for the player to move
                values[i] = v if g.pending_player() == me else -v
                keys = g.option_keys(me)
                prior: dict = {}
                for k, p in zip(keys, torch.softmax(logits[j, :len(keys)], -1).tolist()):
                    prior[k] = prior.get(k, 0.0) + p  # two copies of one hidden card are one option
                for k, p in prior.items():
                    node.prior.setdefault(k, p)
        for (path, _, _, _), v in zip(leaves, values):
            for node, key, mine in path:
                node.value[key] += 1.0 + (v if mine else -v)  # undo the virtual loss, add the result


# ---------------------------------------------------------------- comparing it with other bots

def _opponent(spec: str, net: PolicyNet) -> Agent:
    if spec in AGENTS:
        return AGENTS[spec]()
    return NeuralAgent(net if spec == "net" else load(spec))


def _games(args):
    """One worker's games: (seat the search bot played, winner, seconds it spent searching)."""
    checkpoint, vs, decks, seeds, kw = args
    from .nn.train import _decks
    torch.set_num_threads(1)
    net = load(checkpoint)
    bot, other = MCTSAgent(net, **kw), _opponent(vs, net)
    out = []
    for seed in seeds:
        seat = seed % 2
        bot.rng.seed(seed)
        clock = [0.0]

        class Timed(Agent):
            def pick(self, game):
                t = time.time()
                i = bot.pick(game)
                clock[0] += time.time() - t
                return i

        agents = (Timed(), other) if seat == 0 else (other, Timed())
        out.append((seat, play_rust(_decks(decks), agents, seed=seed).winner, clock[0]))
    return out


def main(argv=None) -> None:
    from .nn.train import DECKS
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("checkpoint", help="the network the search uses")
    ap.add_argument("--vs", default="net", help="net (the same checkpoint without search), greedy, random, "
                                                "or another .pt checkpoint")
    ap.add_argument("--deck0", default=DECKS[0])
    ap.add_argument("--deck1", default=DECKS[1])
    ap.add_argument("--games", type=int, default=200, help="seats alternate")
    ap.add_argument("--sims", type=int, default=200, help="simulations per decision")
    ap.add_argument("--worlds", type=int, default=32, help="determinizations per decision")
    ap.add_argument("--batch", type=int, default=8, help="leaves per network call")
    ap.add_argument("--c-puct", type=float, default=1.5)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--seed", type=int, default=20_000)
    args = ap.parse_args(argv)

    kw = dict(sims=args.sims, worlds=args.worlds, batch=args.batch, c_puct=args.c_puct)
    seeds = list(range(args.seed, args.seed + args.games))
    parts = [seeds[i::args.workers] for i in range(args.workers) if seeds[i::args.workers]]
    t = time.time()
    with mp.get_context("spawn").Pool(len(parts)) as pool:
        results = [r for part in pool.map(_games, [(args.checkpoint, args.vs, (args.deck0, args.deck1), s, kw)
                                                     for s in parts]) for r in part]
    wins = sum(w == s for s, w, _ in results)
    draws = sum(w is None for _, w, _ in results)
    print(f"search ({args.sims} sims) with {args.checkpoint} vs {args.vs}: wins {wins / len(results):.1%}, "
          f"draws {draws / len(results):.1%} over {len(results)} games in {time.time() - t:.0f}s")
    for seat in (0, 1):
        rows = [r for r in results if r[0] == seat]
        if rows:
            name = (args.deck0, args.deck1)[seat]
            print(f"    as {name.split(') ')[-1]:18} {sum(w == seat for _, w, _ in rows) / len(rows):5.1%} ({len(rows)})")
    print(f"    search time per game: {sum(r[2] for r in results) / len(results):.1f}s")


if __name__ == "__main__":
    main()
