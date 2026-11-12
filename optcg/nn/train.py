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
        saved = json.loads(cache.read_text())
        if saved.get("key") == key:
            return {(a, b): v for a, b, v in saved["results"]}
    t = time.time()
    res = matchup_results(pool, workers, pairings, "greedy", "greedy", None, per, engine, parallel)
    print(f"greedy baseline: {per} games x {len(pairings)} matchups in {time.time() - t:.0f}s", flush=True)
    if cache:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"key": key, "results": [[a, b, v] for (a, b), v in res.items()]}))
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
    net = (load(args.init) if args.init else _new_net(args)).to(device)
    config = net.config()
    if args.decks and not net.deck_cond:
        print("note: this network doesn't read the decklists (an older checkpoint, or --plain)", flush=True)
    opt = torch.optim.Adam(net.parameters(), lr=args.lr)
    snapshots: dict[str, dict] = {"init": _cpu_state(net)} if args.init else {}  # self-play from the start
    reference = None
    if args.reference:
        ref = load(args.reference)
        if ref.config() != config:
            raise SystemExit(f"--reference is a different network ({ref.config()}) from the learner ({config})")
        reference = ref.state_dict()
    sampler = league.MatchupSampler(pairings, floor=args.matchup_floor, temp=args.matchup_temp, decay=args.decay)
    pfsp = league.PFSP(power=args.pfsp_power, decay=args.decay)
    best_state, best = None, float("-inf")
    rng = random.Random(args.seed)
    seed = args.seed
    with _pool(args.workers) as pool:
        baseline = None
        if tables:
            baseline = greedy_baseline(pool, args.workers, decks, args.baseline_games, args.engine, args.parallel,
                                       out / "greedy_baseline.json")
            sampler.set_baseline({k: s / n for k, (s, n) in baseline.items()})
        for it in range(1, args.iters + 1):
            t = time.time()
            state = _cpu_state(net)
            sampler.step()
            pfsp.step()
            snaps = dict(list(snapshots.items())[-args.keep:])
            jobs = assign_games(rng, args.games, sampler, snaps, pfsp, args.greedy_share, best_state is not None,
                                args.best_share, seed)
            seed += args.games
            opponents = {"greedy": "greedy", **snaps}
            if best_state is not None:
                opponents["best"] = best_state
            episodes = [e for part in pool.map(_rollouts, [(j, state, config, opponents, args.engine, args.parallel)
                                                           for j in _split(jobs, args.workers)]) for e in part]
            t_play = time.time() - t
            for ep in episodes:
                score = (ep["reward"] + 1) / 2
                sampler.record(ep["opponent"], ep["matchup"], score)
                if ep["opponent"] in snaps:
                    pfsp.record(ep["opponent"], score)

            obs, acts, old_logp, rets, advs = [], [], [], [], []
            for ep in episodes:
                adv = _advantages(ep, 1.0, args.lam)
                for (o, a, lp, v), A in zip(ep["steps"], adv):
                    obs.append(o)
                    acts.append(a)
                    old_logp.append(lp)
                    advs.append(A)
                    rets.append(A + v)
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

            by_opp: dict[str, list[float]] = {}
            for ep in episodes:
                kind = ep["opponent"] if ep["opponent"] in ("greedy", "best") else "snapshots"
                by_opp.setdefault(kind, []).append(ep["reward"])
            rates = ", ".join(f"vs {k} {sum(r > 0 for r in v) / len(v):.0%} ({len(v)})" for k, v in sorted(by_opp.items()))
            print(f"iter {it}: {len(obs)} decisions, play {t_play:.0f}s, learn {time.time() - t - t_play:.0f}s | "
                  f"{rates} | value loss {value_loss.item():.3f}, entropy {entropy.item():.2f}", flush=True)

            save(net, str(out / "latest.pt"), matchups=pairings, iteration=it)
            if it % args.snapshot_every == 0:
                snapshots[f"iter{it}"] = _cpu_state(net)
            if it % args.eval_every == 0 or it == args.iters:
                meta = dict(matchups=pairings, iteration=it)
                if tables:
                    tbl, res = table_eval(pool, args, decks, _cpu_state(net), config, baseline, it)
                    for m, (s, n) in res.items():
                        sampler.record("greedy", m, s, n)
                    league.save_table(tbl, out / f"table_iter{it:04d}.json")
                    league.save_table(tbl, out / "table_latest.json")
                    print(f"  eval vs greedy, every matchup ({args.table_games} games each):", flush=True)
                    _print_table(tbl, indent="    ")
                    win = tbl["mean_win"]
                    meta.update(eval_vs_greedy=win, mean_edge=tbl["mean_edge"])
                    score = tbl["mean_edge"]
                else:
                    win, draw, by_deck = evaluate(pool, args.workers, pairings, _cpu_state(net), "greedy",
                                                  config, args.eval_games, args.engine, args.parallel)
                    print(f"  eval vs greedy (best move, {args.eval_games} games): wins {win:.1%}, "
                          f"draws {draw:.1%}", flush=True)
                    if len(pairings) > 2:
                        _report(by_deck, indent="      ")
                    meta["eval_vs_greedy"] = win
                    score = win
                if reference is not None:
                    score, draw, by_deck = evaluate(pool, args.workers, pairings, _cpu_state(net), reference,
                                                    config, args.eval_games, args.engine, args.parallel)
                    print(f"  eval vs {args.reference} ({args.eval_games} games): wins {score:.1%}, "
                          f"draws {draw:.1%}", flush=True)
                    if len(pairings) > 2:
                        _report(by_deck, indent="      ")
                    meta["eval_vs_reference"] = score
                save(net, str(out / f"iter{it:04d}.pt"), **meta)
                if score > best:
                    best = score
                    best_state = _cpu_state(net)
                    sampler.reset("best")
                    save(net, str(out / "best.pt"), **meta)
                    print(f"  new best: {out / 'best.pt'} (iteration {it})", flush=True)


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
