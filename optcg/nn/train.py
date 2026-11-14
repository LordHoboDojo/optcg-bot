"""Train and evaluate neural bots. Run with the project venv:

    .venv/bin/python -m optcg.nn.train imitate --out runs/imitate.pt
    .venv/bin/python -m optcg.nn.train ppo --init runs/imitate.pt --out runs/ppo --iters 200
    .venv/bin/python -m optcg.nn.train eval runs/ppo/latest.pt --vs greedy --games 400

imitate  learns to copy GreedyAgent's choices (a fast, sane starting point)
ppo      self-play actor-critic (PPO): the bot plays games against its own earlier snapshots and,
         for --greedy-share of them, GreedyAgent, and learns from wins and losses. Decks default
         to your Rocks list vs Linlin; the bot plays both seats, so it learns both decks.
         --reference judges each evaluation head-to-head against a fixed checkpoint, and
         best.pt keeps the checkpoint that did best (beating GreedyAgent alone says little
         once a bot wins most of those games):

    .venv/bin/python -m optcg.nn.train ppo --init runs/ppo/latest.pt --reference runs/ppo/latest.pt --out runs/selfplay
eval     win rate of a checkpoint against an agent (or another checkpoint), both seats

--decks FILE trains one bot on a league of decks (decks/league15.txt: 15 official decks, so 225
matchups counting mirrors). New networks then read their own decklist and the opponent's Leader
(model.py), matchups where the bot's edge over greedy and over the best checkpoint is smallest
come up most (league.MatchupSampler), and snapshot opponents are picked by how often they still
beat the bot (league.PFSP). Each evaluation plays every matchup against greedy and prints and
saves the matchup table (table_latest.json; tools/matchup_table.py renders it):

    .venv/bin/python -m optcg.nn.train imitate --decks decks/league15.txt --out runs/league15_imitate.pt
    .venv/bin/python -m optcg.nn.train ppo --decks decks/league15.txt --init runs/league15_imitate.pt --out runs/league15
    .venv/bin/python -m optcg.nn.train eval runs/league15/best.pt --decks decks/league15.txt --table runs/league15/table.json

--resume continues the run in --out from its files, on this machine or another one (copy the
run directory over): latest.pt (with the optimizer state), the snapshot pool from its iterNNNN.pt
files, best.pt, the last matchup table, and the exploiters and meta-game (league.json). Pass the
run's other flags again; --iters is the total:

    .venv/bin/python -m optcg.nn.train ppo --decks decks/league15.txt --out runs/league15 --iters 500 --resume

Phase 2 adds AlphaStar's exploiters and a PSRO-style Nash ranking of league members
(docs/architecture/python.md has the details):

    .venv/bin/python -m optcg.nn.train ppo --decks decks/league15.txt --init runs/league15_lr/best.pt \
        --pool runs/league15_lr --out runs/league15_p2 --iters 300 --lr 1e-4 --entropy 0.001 \
        --main-exploiters 1 --league-exploiters 1 --exploit-targets runs/league15_lr/final_best.json --nash

--league instead pairs every official deck the engine fully implements (on the Rust engine, all
of them), with no matchup table: too many matchups for one.

Games run in parallel worker processes (--workers, default: CPU count - 1); learning runs on
--device (default: the Apple GPU when available, else the CPU). Games use the Rust engine when
it is built (engine-rs/), the Python one otherwise; --engine picks. Both play the same games.
"""
from __future__ import annotations

import argparse
import json
import multiprocessing as mp
import os
import random
import time
from pathlib import Path

import torch
from torch.nn import functional as F

from ..agents import AGENTS, GreedyAgent
from ..env import Env, encode, encode_rust, play_rust, rust_available, rust_game
from ..play import play_game, resolve_deck
from . import league
from .model import NeuralAgent, PolicyNet, collate, conditioned, from_state, load, save

DECKS = ("rocks", "(Yellow) Charlotte Linlin")


def covered_decks(engine: str | None = None) -> tuple[str, ...]:
    """Official decks the engine ("rust" or "python"; default: Rust when it is built) carries out in
    full. Anything less would train on missing effects. Rust implements every card."""
    from ..carddb import coverage, official_decks
    from ..env import rust_unimplemented
    engine = engine or ("rust" if rust_available() else "python")
    unimplemented = rust_unimplemented if engine == "rust" else None
    return tuple(sorted(n for n, d in official_decks().items()
                        if (c := coverage(d, unimplemented))[0] == c[1]))


def deck_list(args) -> list[str]:
    """The decks a run uses: --decks FILE, every covered deck with --league, else --deck0/--deck1."""
    if getattr(args, "decks", None):
        return league.read_decks(args.decks)
    if getattr(args, "league", False):
        names = list(covered_decks(getattr(args, "engine", None)))
        if len(names) < 2:
            raise SystemExit(f"--league needs 2 or more fully implemented decks, found {len(names)}")
        return names
    return [args.deck0, args.deck1]


def matchups(args) -> list[tuple[str, str]]:
    """The (learner's deck, opponent's deck) pairs to train on. A deck file includes mirrors; one
    matchup (--deck0/--deck1) and --league don't."""
    return league.ordered_matchups(deck_list(args), mirrors=bool(getattr(args, "decks", None)))


def _name(matchup: tuple[str, str]) -> str:
    return " vs ".join(league.short(s) for s in matchup)


# ---------------------------------------------------------------- workers
#
# A job is one game: (seed, my deck, their deck, the learner's seat, opponent key). The opponents
# map keys to 'greedy', 'random' or a network's weights; every network shares one config.

_cache: dict = {}


def _init_worker() -> None:
    torch.set_num_threads(1)


def _decks(specs):
    if specs not in _cache:
        _cache[specs] = tuple(resolve_deck(s) for s in specs)
    return _cache[specs]


def _seated(mine: str, theirs: str, seat: int) -> tuple[str, str]:
    """The decks in seat order, with the learner's in `seat`."""
    return (mine, theirs) if seat == 0 else (theirs, mine)


def _net(state, config):
    return from_state(state, config)


def _imitation_games(args):
    jobs, engine = args
    data = []
    for seed, mine, theirs, seat, _ in jobs:
        decks = _decks(_seated(mine, theirs, seat))
        if engine == "rust":
            g = rust_game(decks, seed)
            while not g.over:
                k = g.greedy()
                data.append((encode_rust(g), k))
                g.step(k)
            continue
        greedy = GreedyAgent()

        class Recorder(GreedyAgent):
            def act(self, g, d):
                a = greedy.act(g, d)
                data.append((encode(g, d), d.options.index(a)))
                return a
        play_game(decks, (Recorder(), Recorder()), seed=seed)
    return data


@torch.no_grad()
def _decide(net, games, sample: bool, record: bool) -> None:
    """Answer the pending decision of every game in `games` with one batched call of `net`."""
    logits, values = net(collate([g["obs"] for g in games]))
    if sample:
        dist = torch.distributions.Categorical(logits=logits)  # padded options are -inf
        acts = dist.sample()
        logps = dist.log_prob(acts)
    else:
        acts = logits.argmax(-1)
    for i, g in enumerate(games):
        a = int(acts[i])
        if record:
            g["steps"].append((g["obs"], a, float(logps[i]), float(values[i])))
        g["obs"], _, _ = g["env"].step(a)


def _play_batched(jobs, net, sample, opponents, opp_sample, config, engine, parallel, record):
    """Play `jobs`, up to `parallel` at a time. Each round, the learner's pending decisions across
    all games go through `net` in one batched call, and each network opponent's likewise; greedy
    and random opponents answer inside the game. One network call per decision would cost
    ~0.4 ms; batched, ~0.05 ms.

    Returns one dict per game in job order: matchup (mine, theirs), seat, opponent key, the Env,
    and with `record` the learner's steps as (obs, action, log prob, value)."""
    opp_nets: dict = {}
    queue, live, finished = list(enumerate(jobs)), [], []

    def start(i, job):
        seed, mine, theirs, seat, key = job
        spec = opponents[key]
        if not isinstance(spec, str) and key not in opp_nets:
            opp_nets[key] = _net(spec, config)
        env = Env(*_decks(_seated(mine, theirs, seat)), opponent=AGENTS[spec]() if isinstance(spec, str) else None,
                  seat=seat, engine=engine)
        return {"i": i, "seed": seed, "env": env, "obs": env.reset(seed), "k": key, "matchup": (mine, theirs),
                "seat": seat, "opponent": key, "steps": []}

    while queue or live:
        while queue and len(live) < parallel:
            g = start(*queue.pop(0))
            (finished if g["env"].done else live).append(g)
        mine = [g for g in live if g["env"].to_move == g["seat"]]
        if mine:
            _decide(net, mine, sample, record)
        theirs: dict = {}
        for g in live:
            if not g["env"].done and g["env"].to_move != g["seat"]:
                theirs.setdefault(g["k"], []).append(g)
        for k, games in theirs.items():
            _decide(opp_nets[k], games, opp_sample, False)
        finished += [g for g in live if g["env"].done]
        live = [g for g in live if not g["env"].done]
    return sorted(finished, key=lambda g: g["i"])


def _rollouts(args):
    """Play games with the learner sampling its moves; returns one episode per job."""
    jobs, state, config, opponents, engine, parallel = args
    games = _play_batched(jobs, _net(state, config), True, opponents, True, config, engine, parallel, record=True)
    return [{"steps": g["steps"], "reward": g["env"].reward(), "matchup": g["matchup"], "seat": g["seat"],
             "opponent": g["opponent"]} for g in games]


def _score(winner, seat) -> float:
    return 0.5 if winner is None else float(winner == seat)


def _eval_games(args):
    """Returns [(matchup, seat, score)] per job, the score 1 for a win of `a`, 1/2 for a draw.
    Networks play their best-scored move."""
    jobs, a_spec, b_spec, config, engine, parallel = args
    if isinstance(a_spec, str):  # no network to batch: play the games one by one
        a = AGENTS[a_spec]()
        b = AGENTS[b_spec]() if isinstance(b_spec, str) else NeuralAgent(_net(b_spec, config))
        play = play_rust if engine == "rust" else play_game
        out = []
        for seed, mine, theirs, seat, _ in jobs:
            g = play(_decks(_seated(mine, theirs, seat)), (a, b) if seat == 0 else (b, a), seed=seed)
            out.append(((mine, theirs), seat, _score(g.winner, seat)))
        return out
    jobs = [(*j[:4], "b") for j in jobs]  # every game is against b
    games = _play_batched(jobs, _net(a_spec, config), False, {"b": b_spec}, False, config, engine, parallel,
                          record=False)
    return [(g["matchup"], g["seat"], _score(g["env"].game.winner, g["seat"])) for g in games]


def _device(name: str) -> torch.device:
    if name == "auto":
        name = "mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu"
    return torch.device(name)


def _cpu_state(net) -> dict:
    """A copy of the weights for the worker processes, which play on the CPU."""
    return {k: v.detach().cpu().clone() for k, v in net.state_dict().items()}


def _to_cpu(x):
    """Tensors anywhere in a (nested) dict or list moved to the CPU, so a checkpoint loads on any device."""
    if torch.is_tensor(x):
        return x.detach().cpu()
    if isinstance(x, dict):
        return {k: _to_cpu(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return type(x)(_to_cpu(v) for v in x)
    return x


def _saved_run(out: Path, args, tables: bool):
    """What --resume picks up from a run directory: the iteration latest.pt was saved at, its
    optimizer state (None in checkpoints from before --resume), the snapshot pool rebuilt from the
    iterNNNN.pt files, and the best checkpoint with the score it was kept for."""
    latest = out / "latest.pt"
    if not latest.exists():
        raise SystemExit(f"--resume: {latest} doesn't exist")
    ck = torch.load(latest, map_location="cpu", weights_only=False)
    it = ck.get("iteration", 0)
    snapshots = {}
    for n in range(args.snapshot_every, it + 1, args.snapshot_every):
        f = out / f"iter{n:04d}.pt"
        if f.exists():
            snapshots[f"iter{n}"] = torch.load(f, map_location="cpu", weights_only=False)["state"]
    best_state, best = None, float("-inf")
    if (out / "best.pt").exists():
        b = torch.load(out / "best.pt", map_location="cpu", weights_only=False)
        key = "eval_vs_reference" if args.reference else "mean_edge" if tables else "eval_vs_greedy"
        if b.get(key) is not None:
            best_state, best = b["state"], b[key]
    return it, ck.get("optimizer"), snapshots, best_state, best


def _to(batch: dict, device) -> dict:
    return {k: v.to(device) for k, v in batch.items()}


def _split(items, n):
    return [items[i::n] for i in range(n) if items[i::n]]


def _pool(workers):
    return mp.get_context("spawn").Pool(workers, initializer=_init_worker)


def _round_robin(pairings, games: int, seed0: int, opp="b") -> list[tuple]:
    """`games` jobs cycling through the matchups, each matchup's games alternating seats. The
    matchups go in a shuffled order (fixed by seed0), so fewer games than matchups still spread
    over every deck."""
    order = list(pairings)
    random.Random(seed0).shuffle(order)
    m = len(order)
    return [(seed0 + i, *order[i % m], (i // m) % 2, opp) for i in range(games)]


# ---------------------------------------------------------------- self-play opponents

def assign_games(rng: random.Random, n: int, sampler: league.MatchupSampler, snapshots: dict,
                 pfsp: league.PFSP, greedy_share: float, best: bool, best_share: float, seed0: int) -> list[tuple]:
    """The jobs of one PPO iteration. Each game's matchup comes from `sampler`, its seat is random,
    and its opponent is GreedyAgent for about `greedy_share` of the games, the best checkpoint
    (when there is one) for `best_share`, and otherwise a snapshot picked by PFSP. With no
    snapshots yet, the games that would have gone to them go to greedy."""
    keys = list(snapshots)
    snap_w = pfsp.weights(keys) if keys else []
    best_p = best_share if best else 0.0
    jobs = []
    for i, (mine, theirs) in enumerate(sampler.sample(rng, n)):
        r = rng.random()
        if r < greedy_share or (not keys and r >= greedy_share + best_p):
            key = "greedy"
        elif r < greedy_share + best_p:
            key = "best"
        else:
            key = rng.choices(keys, weights=snap_w)[0]
        jobs.append((seed0 + i, mine, theirs, rng.randrange(2), key))
    return jobs


# ---------------------------------------------------------------- evaluation

def evaluate(pool, workers, pairings, a, b, config, games, engine="python", parallel=16, seed0=10_000):
    """Share of games `a` wins against `b` (and draws), cycling through the matchups with seats
    alternating. Also returns the win rate per deck `a` played, which is what a league run wants to see."""
    jobs = _round_robin(pairings, games, seed0)
    results = [r for part in pool.map(_eval_games, [(j, a, b, config, engine, parallel)
                                                    for j in _split(jobs, workers)]) for r in part]
    by_deck: dict = {}
    for (mine, _), _, score in results:
        row = by_deck.setdefault(mine, [0.0, 0])
        row[0] += score == 1
        row[1] += 1
    wins = sum(score == 1 for _, _, score in results)
    draws = sum(score == 0.5 for _, _, score in results)
    return wins / games, draws / games, {k: (w / n, n) for k, (w, n) in by_deck.items()}


def matchup_results(pool, workers, pairings, a, b, config, per: int, engine="python", parallel=16,
                    seed0=20_000) -> dict:
    """{matchup: [score, games]} with `per` games of `a` (my deck) against `b` (theirs) in every
    matchup, seats alternating. Seeds depend only on the matchup's place and seed0, so a learner
    and the greedy baseline play the same deals."""
    jobs = [(seed0 + i * per + j, mine, theirs, j % 2, "b")
            for i, (mine, theirs) in enumerate(pairings) for j in range(per)]
    out: dict = {}
    for part in pool.map(_eval_games, [(j, a, b, config, engine, parallel) for j in _split(jobs, workers)]):
        for matchup, _, score in part:
            row = out.setdefault(tuple(matchup), [0.0, 0])
            row[0] += score
            row[1] += 1
    return out


def greedy_baseline(pool, workers, decks, per, engine, parallel, cache: Path | None = None) -> dict:
    """Greedy vs greedy in every matchup of `decks` (the reference edges are measured from), cached
    in `cache` since it never changes for the same decks, games and engine."""
    pairings = league.ordered_matchups(decks)
    key = {"decks": decks, "games": per, "engine": engine}
    if cache and cache.is_file():
        saved = json.loads(cache.read_text(encoding="utf-8"))
        if saved.get("key") == key:
            return {(a, b): v for a, b, v in saved["results"]}
    t = time.time()
    res = matchup_results(pool, workers, pairings, "greedy", "greedy", None, per, engine, parallel)
    print(f"greedy baseline: {per} games x {len(pairings)} matchups in {time.time() - t:.0f}s", flush=True)
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"key": key, "results": [[a, b, v] for (a, b), v in res.items()]}), encoding="utf-8")
    return res


def table_eval(pool, args, decks, state, config, baseline, iteration=None) -> dict:
    """The learner against greedy in every matchup, as a league.table() with the greedy baseline."""
    pairings = league.ordered_matchups(decks)
    res = matchup_results(pool, args.workers, pairings, state, "greedy", config, args.table_games, args.engine,
                          args.parallel)
    meta = {"games_per_matchup": args.table_games, "engine": args.engine}
    if iteration is not None:
        meta["iteration"] = iteration
    return league.table(decks, res, baseline, **meta), res


def _report(by_deck: dict, indent: str = "    ") -> None:
    for deck, (rate, n) in sorted(by_deck.items(), key=lambda kv: kv[1][0]):
        print(f"{indent}as {league.short(deck):18} {rate:5.1%} ({n})")


def _print_table(t: dict, indent: str = "  ") -> None:
    print(indent + league.summary(t))
    for line in league.format_table(t).splitlines():
        print(indent + line)
    if "edge" in t:
        cells = [(t["edge"][i][j], i, j) for i in range(len(t["decks"])) for j in range(len(t["decks"]))
                 if t["edge"][i][j] is not None]
        worst = sorted(cells)[:3]
        print(indent + "smallest edges: " + ", ".join(
            f"{_name((t['decks'][i], t['decks'][j]))} {e:+.0%}" for e, i, j in worst), flush=True)


# ---------------------------------------------------------------- commands

def _new_net(args) -> PolicyNet:
    return PolicyNet(args.hidden) if args.plain else conditioned(args.hidden)


def imitate(args) -> None:
    pairings = matchups(args)
    device = _device(args.device)
    net = _new_net(args).to(device)
    config = net.config()
    opt = torch.optim.Adam(net.parameters(), lr=args.lr)
    with _pool(args.workers) as pool:
        t = time.time()
        jobs = _round_robin(pairings, args.games, 0, None)
        data = [x for part in pool.map(_imitation_games, [(j, args.engine) for j in _split(jobs, args.workers)])
                for x in part]
        print(f"{len(data)} decisions from {args.games} greedy games over "
              f"{len(pairings)} matchup(s) in {time.time() - t:.0f}s")
        rng = random.Random(0)
        rng.shuffle(data)
        held = data[:len(data) // 20]
        train = data[len(data) // 20:]
        for epoch in range(args.epochs):
            net.train()
            rng.shuffle(train)
            for i in range(0, len(train), args.batch):
                chunk = train[i:i + args.batch]
                logits, _ = net(_to(collate([o for o, _ in chunk]), device))
                loss = F.cross_entropy(logits, torch.tensor([a for _, a in chunk], device=device))
                opt.zero_grad()
                loss.backward()
                opt.step()
            net.eval()
            with torch.no_grad():
                acc = torch.cat([net(_to(collate([o for o, _ in held[i:i + 1024]]), device))[0].argmax(1).cpu()
                                 for i in range(0, len(held), 1024)]) == torch.tensor([a for _, a in held])
                acc = acc.float().mean()
            print(f"epoch {epoch + 1}: loss {loss.item():.3f}, held-out agreement with greedy {acc:.1%}")
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        net.cpu()
        save(net, args.out, matchups=pairings)
        state = _cpu_state(net)
        for vs in ("random", "greedy"):
            win, _, by_deck = evaluate(pool, args.workers, pairings, state, vs, config, args.eval_games,
                                       args.engine, args.parallel)
            print(f"vs {vs}: wins {win:.1%}")
            if len(pairings) > 2:
                _report(by_deck)
        if args.decks:
            decks = deck_list(args)
            baseline = greedy_baseline(pool, args.workers, decks, args.baseline_games, args.engine, args.parallel,
                                       Path(args.out).parent / "greedy_baseline.json")
            tbl, _ = table_eval(pool, args, decks, state, config, baseline)
            league.save_table(tbl, Path(args.out).with_suffix(".table.json"))
            print(f"vs greedy, every matchup ({args.table_games} games each):")
            _print_table(tbl)
    print(f"saved {args.out}")


def _advantages(ep, gamma, lam):
    """GAE over one episode; the only reward comes at the end."""
    steps = ep["steps"]
    adv = [0.0] * len(steps)
    nxt, running = 0.0, 0.0
    for t in reversed(range(len(steps))):
        r = ep["reward"] if t == len(steps) - 1 else 0.0
        v = steps[t][3]
        delta = r + gamma * nxt - v
        running = delta + gamma * lam * running
        adv[t] = running
        nxt = v
    return adv


def _ppo_update(net, opt, episodes, args, device) -> tuple[int, float, float]:
    """One PPO update of `net` on `episodes`; returns the decisions learned from, the last value
    loss and the last entropy."""
    obs, acts, old_logp, rets, advs = [], [], [], [], []
    for ep in episodes:
        adv = _advantages(ep, 1.0, args.lam)
        for (o, a, lp, v), A in zip(ep["steps"], adv):
            obs.append(o)
            acts.append(a)
            old_logp.append(lp)
            advs.append(A)
            rets.append(A + v)
    if not obs:
        return 0, float("nan"), float("nan")
    acts_t, old_t = torch.tensor(acts, device=device), torch.tensor(old_logp, device=device)
    adv_t, ret_t = torch.tensor(advs, device=device), torch.tensor(rets, device=device)
    adv_t = (adv_t - adv_t.mean()) / (adv_t.std() + 1e-8)
    net.train()
    idx = list(range(len(obs)))
    for _ in range(args.epochs):
        random.shuffle(idx)
        for i in range(0, len(idx), args.batch):
            mb = idx[i:i + args.batch]
            logits, value = net(_to(collate([obs[j] for j in mb]), device))
            dist = torch.distributions.Categorical(logits=logits)
            logp = dist.log_prob(acts_t[mb])
            ratio = (logp - old_t[mb]).exp()
            a = adv_t[mb]
            policy_loss = -torch.min(ratio * a, ratio.clamp(1 - args.clip, 1 + args.clip) * a).mean()
            value_loss = F.mse_loss(value, ret_t[mb])
            entropy = dist.entropy().mean()
            loss = policy_loss + 0.5 * value_loss - args.entropy * entropy
            opt.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(net.parameters(), 1.0)
            opt.step()
    net.eval()
    return len(obs), value_loss.item(), entropy.item()


def _tasks(jobs, state, config, opponents, args) -> list[tuple]:
    """Rollout tasks for the workers, each carrying only the opponents its own games use."""
    return [(part, state, config, {k: opponents[k] for k in {j[4] for j in part}}, args.engine, args.parallel)
            for part in _split(jobs, args.workers)]


# ---------------------------------------------------------------- phase 2: exploiters and the Nash ranking

class Exploiter:
    """A learner that trains to beat the league rather than to play well (AlphaStar's exploiters).
    A main exploiter ("main") plays only the current main agent, mostly in the main agent's
    weakest matchups (it takes the opponent's deck there); a league exploiter ("league") plays
    the frozen pool, main snapshots and exploiter copies, picked by PFSP, in any matchup. When it
    wins `--promote-at` of its recent games after at least `--exploiter-min-iters` iterations, or
    its `--exploiter-budget` runs out, a frozen copy joins the main agent's opponents and it starts
    a new generation: a main exploiter from its initial weights, a league exploiter from them
    with probability `--league-reset` (otherwise it carries on)."""

    def __init__(self, name: str, kind: str, init_state: dict, config, device, args):
        self.name, self.kind = name, kind
        self.init_state, self.config, self.device, self.args = init_state, config, device, args
        self.gen = 1
        self.reset()

    def reset(self) -> None:
        self.net = from_state(self.init_state, self.config).to(self.device)
        self.opt = torch.optim.Adam(self.net.parameters(), lr=self.args.lr)
        self.restart()

    def restart(self) -> None:
        """A new generation's bookkeeping, keeping the weights."""
        self.iters = 0
        self.rates = league._Rates(self.args.decay, 4.0)
        self.pfsp = league.PFSP(power=self.args.pfsp_power, decay=self.args.decay)

    def win_rate(self) -> float:
        return self.rates.rate("all")

    def ready(self) -> bool:
        a = self.args
        return ((self.iters >= a.exploiter_min_iters and self.win_rate() >= a.promote_at)
                or self.iters >= a.exploiter_budget)

    def copy_name(self) -> str:
        return f"{self.name}.g{self.gen}"

    def jobs(self, rng: random.Random, n: int, pairings, targets, pool_keys: list[str], seed0: int) -> list[tuple]:
        jobs = []
        weights = self.pfsp.weights(pool_keys) if pool_keys else []
        for i in range(n):
            if self.kind == "main":
                theirs, mine = rng.choice(targets) if targets and rng.random() < self.args.exploit_focus \
                    else rng.choice(pairings)  # the main agent plays the first deck of its weak matchup
                key = "main"
            else:
                mine, theirs = rng.choice(pairings)
                key = rng.choices(pool_keys, weights=weights)[0] if pool_keys else "main"
            jobs.append((seed0 + i, mine, theirs, rng.randrange(2), key))
        return jobs

    def record(self, episodes) -> None:
        for ep in episodes:
            score = (ep["reward"] + 1) / 2
            self.rates.record("all", score)
            if self.kind == "league":
                self.pfsp.record(ep["opponent"], score)

    def step(self) -> None:
        self.rates.step()
        self.pfsp.step()

    def save(self, path: Path, **meta) -> None:
        save(self.net, str(path), kind=self.kind, gen=self.gen, iters=self.iters, rates=self.rates.counts,
             pfsp=self.pfsp.rates.counts, optimizer=_to_cpu(self.opt.state_dict()), **meta)

    def load(self, path: Path) -> None:
        ck = torch.load(path, map_location="cpu", weights_only=False)
        self.net = from_state(ck["state"], ck["config"]).to(self.device)
        self.opt = torch.optim.Adam(self.net.parameters(), lr=self.args.lr)
        self.opt.load_state_dict(ck["optimizer"])
        for g in self.opt.param_groups:
            g["lr"] = self.args.lr
        self.gen, self.iters = ck["gen"], ck["iters"]
        self.rates.counts, self.pfsp.rates.counts = ck["rates"], ck["pfsp"]


def play_pairs(pool, args, pairs, specs: dict, config, pairings, games: int, seed0: int) -> dict:
    """Head-to-head games for each (a, b) in `pairs` (specs: name -> weights or 'greedy'; `a` must
    be a network): `games` games in random matchups, each played with the decks both ways round so
    deck strength cancels, seats alternating. Returns {(a, b): [a's score, games]}."""
    rng = random.Random(seed0)
    tasks, owners = [], []
    for a, b in pairs:
        jobs = []
        for k in range(max(1, games // 2)):
            x, y = rng.choice(pairings)
            s = seed0 + 2 * len(owners) * games + 2 * k
            jobs += [(s, x, y, k % 2, "b"), (s + 1, y, x, 1 - k % 2, "b")]
        for part in _split(jobs, args.workers):
            tasks.append((part, specs[a], specs[b], config, args.engine, args.parallel))
            owners.append((a, b))
    out = {p: [0.0, 0] for p in pairs}
    for owner, part in zip(owners, pool.map(_eval_games, tasks) if tasks else []):
        for _, _, score in part:
            out[owner][0] += score
            out[owner][1] += 1
    return out


def _fill_meta(pool, args, meta: league.MetaGame, specs: dict, config, pairings, seed0: int) -> int:
    """Play every pair of members that has fewer than --nash-games games; returns the games played."""
    pairs = []
    for i, a in enumerate(meta.members):
        for b in meta.members[i + 1:]:
            if meta.games(a, b) < args.nash_games:
                pairs.append((a, b) if specs[a] != "greedy" else (b, a))
    if not pairs:
        return 0
    res = play_pairs(pool, args, pairs, specs, config, pairings, args.nash_games, seed0)
    for (a, b), (s, n) in res.items():
        meta.record(a, b, s, n)
    return sum(n for _, n in res.values())


def _pool_snapshots(run: Path, keep: int) -> dict:
    """The last `keep` iterNNNN.pt checkpoints of another run, as snapshot opponents."""
    files = sorted(run.glob("iter[0-9][0-9][0-9][0-9].pt"))[-keep:]
    if not files:
        raise SystemExit(f"--pool: no iterNNNN.pt checkpoints in {run}")
    return {f"pool.{f.stem}": torch.load(f, map_location="cpu", weights_only=False)["state"] for f in files}


def _save_state(state: dict, config, path: Path, **meta) -> None:
    save(from_state(state, config), str(path), **meta)


def ppo(args) -> None:
    if not 0 <= args.greedy_share < 1 or not 0 <= args.best_share < 1 or args.greedy_share + args.best_share >= 1:
        raise SystemExit("--greedy-share and --best-share must be at least 0 and add up to less than 1")
    decks = deck_list(args)
    pairings = matchups(args)
    tables = bool(args.decks)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if len(pairings) > 2:
        print(f"league of {len(decks)} decks, {len(pairings)} matchups", flush=True)
    device = _device(args.device)
    start, opt_state = 0, None
    if args.resume:
        start, opt_state, saved, best_state, best = _saved_run(out, args, tables)
        net = load(str(out / "latest.pt")).to(device)
    else:
        net = (load(args.init) if args.init else _new_net(args)).to(device)
        best_state, best = None, float("-inf")
    config = net.config()
    if args.decks and not net.deck_cond:
        print("note: this network doesn't read the decklists (an older checkpoint, or --plain)", flush=True)
    opt = torch.optim.Adam(net.parameters(), lr=args.lr)
    init_state = None
    if args.init:
        init_state = load(args.init).state_dict() if args.resume else _cpu_state(net)
    snapshots: dict[str, dict] = {}  # self-play from the start
    if init_state is not None:
        snapshots["init"] = init_state
    if args.pool:
        snapshots.update(_pool_snapshots(Path(args.pool), args.keep))
    if args.resume:
        snapshots.update(saved)
        if opt_state is not None:
            opt.load_state_dict(opt_state)
            for g in opt.param_groups:  # the command line's --lr wins over the saved one
                g["lr"] = args.lr
        print(f"resuming {out} after iteration {start}: {len(saved)} snapshots, "
              f"{'the best at ' + format(best, '+.1%') if best_state is not None else 'no best yet'}, "
              f"{'saved' if opt_state is not None else 'fresh'} optimizer state", flush=True)
        if start >= args.iters:
            raise SystemExit(f"--resume: the run is already at iteration {start}; raise --iters")
    reference = None
    if args.reference:
        ref = load(args.reference)
        if ref.config() != config:
            raise SystemExit(f"--reference is a different network ({ref.config()}) from the learner ({config})")
        reference = ref.state_dict()

    # phase 2: exploiters, their frozen copies, and the Nash meta-game over league members
    ex_init = load(args.exploiter_init).state_dict() if args.exploiter_init else \
        init_state if init_state is not None else _cpu_state(net)
    exploiters = ([Exploiter(f"mx{i + 1}", "main", ex_init, config, device, args) for i in range(args.main_exploiters)]
                  + [Exploiter(f"lx{i + 1}", "league", ex_init, config, device, args)
                     for i in range(args.league_exploiters)])
    copies: dict[str, dict] = {}
    targets: list[tuple[str, str]] = []
    if args.exploit_targets:
        t = json.loads(Path(args.exploit_targets).read_text(encoding="utf-8"))
        targets = [m for m in league.weakest(t, args.exploit_cells) if m in pairings]
    meta = league.MetaGame() if args.nash else None
    specs: dict[str, object] = {"greedy": "greedy"}
    best_name = None
    if meta is not None:
        meta.add("greedy", "greedy")
        if init_state is not None:
            meta.add("init", "main")
            specs["init"] = init_state
            best_name, best_state = "init", init_state
            if not args.resume:  # the starting checkpoint is the best until a main checkpoint outranks it
                _save_state(init_state, config, out / "best.pt", best="init", iteration=0)
    saved_league = out / "league.json"
    if args.resume and saved_league.exists():
        sl = json.loads(saved_league.read_text(encoding="utf-8"))
        for name in sl.get("copies", []):
            copies[name] = torch.load(out / f"{name}.pt", map_location="cpu", weights_only=False)["state"]
        targets = [tuple(m) for m in sl.get("targets", targets)]
        for ex in exploiters:
            if (out / f"{ex.name}.pt").exists():
                ex.load(out / f"{ex.name}.pt")
        if meta is not None and sl.get("meta"):
            meta = league.MetaGame.from_json(sl["meta"])
            for m in list(meta.members):
                if m == "greedy":
                    continue
                f = out / (f"iter{int(m[4:]):04d}.pt" if m.startswith("iter") else f"{m}.pt")
                if m in copies:
                    specs[m] = copies[m]
                elif m == "init" and init_state is not None:
                    specs[m] = init_state
                elif f.exists():
                    specs[m] = torch.load(f, map_location="cpu", weights_only=False)["state"]
                else:
                    meta.remove(m)
            if sl.get("best") in specs:
                best_name, best_state = sl["best"], specs[sl["best"]]
    if exploiters:
        print(f"exploiters: {', '.join(f'{e.name} ({e.kind}, generation {e.gen})' for e in exploiters)}; "
              f"{len(targets)} target matchups", flush=True)

    sampler = league.MatchupSampler(pairings, floor=args.matchup_floor, temp=args.matchup_temp, decay=args.decay)
    pfsp = league.PFSP(power=args.pfsp_power, decay=args.decay)
    rng = random.Random(args.seed + 1_000_003 * start)
    per_iter = args.games + args.exploiter_games * len(exploiters)
    seed = args.seed + start * per_iter

    def save_league():
        saved_league.write_text(json.dumps({
            "copies": list(copies), "targets": [list(m) for m in targets], "best": best_name,
            "meta": meta.to_json() if meta is not None else None}), encoding="utf-8")

    with _pool(args.workers) as pool:
        baseline = None
        if tables:
            baseline = greedy_baseline(pool, args.workers, decks, args.baseline_games, args.engine, args.parallel,
                                       out / "greedy_baseline.json")
            sampler.set_baseline({k: s / n for k, (s, n) in baseline.items()})
            last = out / "table_latest.json"
            if args.resume and last.exists():  # the last table's results, as if just measured
                t = json.loads(last.read_text(encoding="utf-8"))
                for a, row in zip(t["decks"], t["win"]):
                    for b, w in zip(t["decks"], row):
                        if w is not None and (a, b) in pairings:
                            sampler.record("greedy", (a, b), w * args.table_games, args.table_games)
        for it in range(start + 1, args.iters + 1):
            t = time.time()
            state = _cpu_state(net)
            sampler.step()
            pfsp.step()
            for ex in exploiters:
                ex.step()
            snaps = dict(list(snapshots.items())[-args.keep:])
            snaps.update(list(copies.items())[-args.keep_exploiters:])
            jobs = assign_games(rng, args.games, sampler, snaps, pfsp, args.greedy_share, best_state is not None,
                                args.best_share, seed)
            seed += args.games
            opponents = {"greedy": "greedy", **snaps}
            if best_state is not None:
                opponents["best"] = best_state
            groups = [_tasks(jobs, state, config, opponents, args)]
            for ex in exploiters:
                ex_jobs = ex.jobs(rng, args.exploiter_games, pairings, targets, list(snaps), seed)
                seed += args.exploiter_games
                groups.append(_tasks(ex_jobs, _cpu_state(ex.net), config, {"main": state, **snaps}, args))
            played = pool.map(_rollouts, [task for g in groups for task in g])
            per_group, k = [], 0
            for g in groups:
                per_group.append([e for part in played[k:k + len(g)] for e in part])
                k += len(g)
            episodes = per_group[0]
            t_play = time.time() - t
            for ep in episodes:
                score = (ep["reward"] + 1) / 2
                sampler.record(ep["opponent"], ep["matchup"], score)
                if ep["opponent"] in snaps:
                    pfsp.record(ep["opponent"], score)

            n_obs, value_loss, entropy = _ppo_update(net, opt, episodes, args, device)
            by_opp: dict[str, list[float]] = {}
            for ep in episodes:
                o = ep["opponent"]
                kind = o if o in ("greedy", "best") else "exploiters" if o in copies else "snapshots"
                by_opp.setdefault(kind, []).append(ep["reward"])
            rates = ", ".join(f"vs {k} {sum(r > 0 for r in v) / len(v):.0%} ({len(v)})" for k, v in sorted(by_opp.items()))
            print(f"iter {it}: {n_obs} decisions, play {t_play:.0f}s, learn {time.time() - t - t_play:.0f}s | "
                  f"{rates} | value loss {value_loss:.3f}, entropy {entropy:.2f}", flush=True)

            for ex, eps in zip(exploiters, per_group[1:]):
                t_ex = time.time()
                ex.record(eps)
                n_ex, _, ent = _ppo_update(ex.net, ex.opt, eps, args, device)
                ex.iters += 1
                won = sum(e["reward"] > 0 for e in eps) / max(1, len(eps))
                print(f"  {ex.copy_name()} ({ex.kind} exploiter, iteration {ex.iters}): won {won:.0%} of {len(eps)} "
                      f"(recent {ex.win_rate():.0%}), {n_ex} decisions, learn {time.time() - t_ex:.0f}s, "
                      f"entropy {ent:.2f}", flush=True)
                if ex.ready():
                    name = ex.copy_name()
                    copies[name] = _cpu_state(ex.net)
                    _save_state(copies[name], config, out / f"{name}.pt", promoted_at=it, win_rate=ex.win_rate())
                    if meta is not None:
                        meta.add(name, "exploiter")
                        specs[name] = copies[name]
                    print(f"  {name} joins the main agent's opponents (recent win rate {ex.win_rate():.0%}, "
                          f"{ex.iters} iterations)", flush=True)
                    ex.gen += 1
                    if ex.kind == "main" or rng.random() < args.league_reset:
                        ex.reset()
                    else:
                        ex.restart()
                ex.save(out / f"{ex.name}.pt", iteration=it)

            save(net, str(out / "latest.pt"), matchups=pairings, iteration=it, optimizer=_to_cpu(opt.state_dict()))
            if it % args.snapshot_every == 0:
                snapshots[f"iter{it}"] = _cpu_state(net)
            if it % args.eval_every == 0 or it == args.iters:
                info = dict(matchups=pairings, iteration=it)
                if tables:
                    tbl, res = table_eval(pool, args, decks, _cpu_state(net), config, baseline, it)
                    for m, (s, n) in res.items():
                        sampler.record("greedy", m, s, n)
                    league.save_table(tbl, out / f"table_iter{it:04d}.json")
                    league.save_table(tbl, out / "table_latest.json")
                    print(f"  eval vs greedy, every matchup ({args.table_games} games each):", flush=True)
                    _print_table(tbl, indent="    ")
                    win = tbl["mean_win"]
                    info.update(eval_vs_greedy=win, mean_edge=tbl["mean_edge"])
                    score = tbl["mean_edge"]
                    if any(ex.kind == "main" for ex in exploiters):
                        targets = league.weakest(tbl, args.exploit_cells)
                else:
                    win, draw, by_deck = evaluate(pool, args.workers, pairings, _cpu_state(net), "greedy",
                                                  config, args.eval_games, args.engine, args.parallel)
                    print(f"  eval vs greedy (best move, {args.eval_games} games): wins {win:.1%}, "
                          f"draws {draw:.1%}", flush=True)
                    if len(pairings) > 2:
                        _report(by_deck, indent="      ")
                    info["eval_vs_greedy"] = win
                    score = win
                if reference is not None:
                    score, draw, by_deck = evaluate(pool, args.workers, pairings, _cpu_state(net), reference,
                                                    config, args.eval_games, args.engine, args.parallel)
                    print(f"  eval vs {args.reference} ({args.eval_games} games): wins {score:.1%}, "
                          f"draws {draw:.1%}", flush=True)
                    if len(pairings) > 2:
                        _report(by_deck, indent="      ")
                    info["eval_vs_reference"] = score
                save(net, str(out / f"iter{it:04d}.pt"), **info)
                if meta is not None:
                    name = f"iter{it}"
                    meta.add(name, "main")
                    specs[name] = _cpu_state(net)
                    t_nash = time.time()
                    n_games = _fill_meta(pool, args, meta, specs, config, pairings, 5_000_000 + it * 100_000)
                    for m in meta.prune(args.nash_members, {"greedy", name, best_name}):
                        specs.pop(m, None)
                    sol = meta.solve()
                    print(f"  Nash ranking ({len(meta.members)} members, {n_games} games, "
                          f"{time.time() - t_nash:.0f}s): {meta.format()}", flush=True)
                    top = meta.ranking("main")[0]
                    if top != best_name:
                        best_name, best_state = top, specs[top]
                        sampler.reset("best")
                        _save_state(best_state, config, out / "best.pt", **info, best=top,
                                    nash_rating=sol[top][1], nash_weight=sol[top][0])
                        print(f"  new best: {top} ({out / 'best.pt'})", flush=True)
                elif score > best:
                    best = score
                    best_state = _cpu_state(net)
                    sampler.reset("best")
                    save(net, str(out / "best.pt"), **info)
                    print(f"  new best: {out / 'best.pt'} (iteration {it})", flush=True)
            if exploiters or meta is not None:
                save_league()


def eval_cmd(args) -> None:
    pairings = matchups(args)
    net = load(args.checkpoint)
    config = net.config()
    with _pool(args.workers) as pool:
        if args.decks and args.vs == "greedy":
            decks = deck_list(args)
            cache = Path(args.baseline) if args.baseline else None
            baseline = greedy_baseline(pool, args.workers, decks, args.baseline_games, args.engine, args.parallel,
                                       cache)
            tbl, _ = table_eval(pool, args, decks, net.state_dict(), config, baseline)
            tbl["checkpoint"] = args.checkpoint
            print(f"{args.checkpoint} vs greedy, every matchup ({args.table_games} games each):")
            _print_table(tbl)
            if args.table:
                league.save_table(tbl, args.table)
                print(f"saved {args.table}")
            return
        vs = args.vs if args.vs in AGENTS else load(args.vs).state_dict()
        win, draw, by_deck = evaluate(pool, args.workers, pairings, net.state_dict(), vs, config, args.games,
                                      args.engine, args.parallel)
    print(f"{args.checkpoint} vs {args.vs}: wins {win:.1%}, draws {draw:.1%} over {args.games} games")
    if len(pairings) > 2:
        _report(by_deck)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--deck0", default=DECKS[0])
        p.add_argument("--deck1", default=DECKS[1])
        p.add_argument("--decks", help="a deck file (one deck per line, e.g. decks/league15.txt): train or "
                                       "evaluate on every matchup of those decks instead of --deck0/--deck1")
        p.add_argument("--league", action="store_true",
                       help="use every pairing of the fully implemented decks instead of --deck0/--deck1")
        p.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
        p.add_argument("--device", default="auto", help="where learning runs: auto, cpu, mps or cuda")
        p.add_argument("--engine", default="auto", choices=("auto", "python", "rust"),
                       help="rules engine for the games: auto uses Rust (optcg_rs) when it is built")
        p.add_argument("--parallel", type=int, default=16,
                       help="games each worker plays at once, so the network scores their moves in one batch")

    def tables(p):
        p.add_argument("--table-games", type=int, default=8,
                       help="with --decks: games per matchup in each matchup-table evaluation")
        p.add_argument("--baseline-games", type=int, default=32,
                       help="with --decks: greedy-vs-greedy games per matchup for the baseline edges are measured from")

    def new_net(p):
        p.add_argument("--hidden", type=int, default=128, help="network width for a new network (e.g. 256)")
        p.add_argument("--plain", action="store_true",
                       help="a new network without card features or deck conditioning (the original one)")

    p = sub.add_parser("imitate")
    common(p)
    new_net(p)
    tables(p)
    p.add_argument("--out", default="runs/imitate.pt")
    p.add_argument("--games", type=int, default=2000)
    p.add_argument("--epochs", type=int, default=5)
    p.add_argument("--batch", type=int, default=256)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--eval-games", type=int, default=200)
    p.set_defaults(fn=imitate)

    p = sub.add_parser("ppo")
    common(p)
    new_net(p)
    tables(p)
    p.add_argument("--out", default="runs/ppo")
    p.add_argument("--init", help="start from this checkpoint (e.g. the imitation one)")
    p.add_argument("--resume", action="store_true",
                   help="continue the run in --out from its latest.pt (on this or another machine): the "
                        "optimizer, the snapshot pool from its iterNNNN.pt files and best.pt carry over; "
                        "pass the run's other flags again, with --iters as the total")
    p.add_argument("--iters", type=int, default=100)
    p.add_argument("--games", type=int, default=128, help="games per iteration")
    p.add_argument("--epochs", type=int, default=3)
    p.add_argument("--batch", type=int, default=512)
    p.add_argument("--lr", type=float, default=3e-4)
    p.add_argument("--clip", type=float, default=0.2)
    p.add_argument("--lam", type=float, default=1.0,
                   help="GAE lambda; 1 = plain win/loss returns, lower trusts the value head more")
    p.add_argument("--entropy", type=float, default=0.01)
    p.add_argument("--snapshot-every", type=int, default=10, help="add a frozen copy to the opponent pool")
    p.add_argument("--keep", type=int, default=20, help="how many recent snapshots stay in the pool")
    p.add_argument("--greedy-share", type=float, default=0.25,
                   help="share of games against GreedyAgent; the rest are against snapshots and the best")
    p.add_argument("--best-share", type=float, default=0.1,
                   help="share of games against the best checkpoint so far, once there is one")
    p.add_argument("--pfsp-power", type=float, default=2.0,
                   help="snapshot opponents are weighted (1 - learner's win rate against them) ** this; 0 = uniform")
    p.add_argument("--matchup-floor", type=float, default=0.3,
                   help="share of games spread evenly over every matchup; the rest favour the smallest edges")
    p.add_argument("--matchup-temp", type=float, default=0.1,
                   help="how sharply matchups with smaller edges are favoured (lower: more)")
    p.add_argument("--decay", type=float, default=0.95,
                   help="per-iteration decay of the win counts matchup and opponent sampling go by")
    p.add_argument("--reference", help="checkpoint to judge evaluations against head-to-head; best.pt keeps "
                                       "the best (without it, the best mean edge over greedy with --decks, "
                                       "else the best win rate against GreedyAgent)")
    p.add_argument("--eval-every", type=int, default=10)
    p.add_argument("--eval-games", type=int, default=200)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--pool", help="a previous run's directory: its last --keep iterNNNN.pt checkpoints start the "
                                  "snapshot pool (e.g. with --init that run's best.pt)")
    x = p.add_argument_group("phase 2: exploiters and the Nash ranking")
    x.add_argument("--main-exploiters", type=int, default=0,
                   help="learners that play only the current main agent, mostly in its weakest matchups")
    x.add_argument("--league-exploiters", type=int, default=0,
                   help="learners that play the frozen pool (snapshots and exploiter copies), picked by PFSP")
    x.add_argument("--exploiter-games", type=int, default=128, help="games per exploiter per iteration")
    x.add_argument("--exploiter-init", help="checkpoint exploiters start (and restart) from; default --init")
    x.add_argument("--exploit-targets", help="a matchup table JSON (e.g. a final eval of --init) whose lowest-edge "
                                             "matchups the main exploiters start on; later tables replace them")
    x.add_argument("--exploit-cells", type=int, default=10, help="how many of the weakest matchups to target")
    x.add_argument("--exploit-focus", type=float, default=0.8,
                   help="share of a main exploiter's games in the target matchups (the rest: any matchup)")
    x.add_argument("--promote-at", type=float, default=0.7,
                   help="an exploiter's recent win rate at which a frozen copy joins the main agent's opponents")
    x.add_argument("--exploiter-min-iters", type=int, default=10, help="iterations before an exploiter can be promoted")
    x.add_argument("--exploiter-budget", type=int, default=100,
                   help="iterations after which an exploiter is promoted and restarted anyway")
    x.add_argument("--league-reset", type=float, default=0.25,
                   help="chance a promoted league exploiter restarts from its initial weights")
    x.add_argument("--keep-exploiters", type=int, default=10,
                   help="how many recent exploiter copies stay in the main agent's opponent pool")
    x.add_argument("--nash", action="store_true",
                   help="rank league members (main checkpoints at each eval, exploiter copies, greedy) by the Nash "
                        "equilibrium of their head-to-head results, and keep the top main checkpoint as best.pt")
    x.add_argument("--nash-games", type=int, default=64, help="head-to-head games per pair of league members")
    x.add_argument("--nash-members", type=int, default=12,
                   help="league members kept in the meta-game; the least needed in the Nash mixture go first")
    p.set_defaults(fn=ppo)

    p = sub.add_parser("eval")
    common(p)
    tables(p)
    p.add_argument("checkpoint")
    p.add_argument("--vs", default="greedy", help="random, greedy, or another checkpoint")
    p.add_argument("--games", type=int, default=400)
    p.add_argument("--table", help="with --decks and --vs greedy: save the matchup table here as JSON")
    p.add_argument("--baseline", help="with --decks: cache file for the greedy baseline (e.g. "
                                      "runs/league15/greedy_baseline.json, which a ppo run writes)")
    p.set_defaults(fn=eval_cmd)

    args = ap.parse_args(argv)
    if args.engine == "auto":
        args.engine = "rust" if rust_available() else "python"
    print(f"engine: {args.engine}", flush=True)
    args.fn(args)


if __name__ == "__main__":
    main()
