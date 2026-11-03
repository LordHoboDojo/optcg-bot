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

--league replaces the single matchup with every pairing of the official decks whose effect text the
engine fully implements (on the Rust engine, all of them), sampled per game, so one bot learns them
all and far more cards see real play:

    .venv/bin/python -m optcg.nn.train imitate --league --out runs/league_imitate.pt
    .venv/bin/python -m optcg.nn.train ppo --league --init runs/league_imitate.pt --out runs/league
    .venv/bin/python -m optcg.nn.train eval runs/league/latest.pt --league --vs greedy

Games run in parallel worker processes (--workers, default: CPU count - 1); learning runs on
--device (default: the Apple GPU when available, else the CPU). Games use the Rust engine when
it is built (engine-rs/), the Python one otherwise; --engine picks. Both play the same games.
"""
from __future__ import annotations

import argparse
import itertools
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
from .model import NeuralAgent, PolicyNet, collate, load, save

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


def matchups(args) -> tuple[tuple[str, str], ...]:
    """The deck pairings to train on: one matchup, or every pairing with --league."""
    if not getattr(args, "league", False):
        return ((args.deck0, args.deck1),)
    names = covered_decks(getattr(args, "engine", None))
    if len(names) < 2:
        raise SystemExit(f"--league needs 2 or more fully implemented decks, found {len(names)}")
    return tuple(itertools.combinations(names, 2))  # each seat is covered by alternating seats


def _name(matchup: tuple[str, str]) -> str:
    return " vs ".join(s.split(") ")[-1] for s in matchup)


# ---------------------------------------------------------------- workers

_cache: dict = {}


def _init_worker() -> None:
    torch.set_num_threads(1)


def _decks(specs):
    if specs not in _cache:
        _cache[specs] = tuple(resolve_deck(s) for s in specs)
    return _cache[specs]


def _assign(pairings, seed: int) -> tuple[tuple[str, str], int]:
    """The matchup and seat a seed plays. The seat comes from a different digit of the seed, so
    with an even number of pairings a deck doesn't always land on the same side."""
    return pairings[seed % len(pairings)], (seed // len(pairings)) % 2


def _net(state, hidden):
    net = PolicyNet(hidden)
    net.load_state_dict(state)
    return net.eval()


def _imitation_games(args):
    pairings, seeds, engine = args
    data = []
    for seed in seeds:
        if engine == "rust":
            g = rust_game(_decks(_assign(pairings, seed)[0]), seed)
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
        play_game(_decks(_assign(pairings, seed)[0]), (Recorder(), Recorder()), seed=seed)
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


def _play_batched(pairings, seeds, net, sample, opponents, opp_sample, hidden, engine, parallel, record):
    """Play the games for `seeds`, up to `parallel` at a time, against an opponent each seed
    picks from `opponents` ('greedy', 'random' or a network's state dict). Each round, the
    learner's pending decisions across all games go through `net` in one batched call, and each
    network opponent's likewise; greedy and random opponents answer inside the game. One network
    call per decision would cost ~0.4 ms; batched, ~0.05 ms.

    Returns one dict per game in seed order: matchup, seat, opponent, the Env, and with
    `record` the learner's steps as (obs, action, log prob, value)."""
    opp_nets: dict = {}
    queue, live, finished = list(seeds), [], []

    def start(seed):
        matchup, seat = _assign(pairings, seed)
        k = random.Random(seed).choice(range(len(opponents)))  # the same pick as choice(opponents)
        spec = opponents[k]
        if not isinstance(spec, str) and k not in opp_nets:
            opp_nets[k] = _net(spec, hidden)
        env = Env(*_decks(matchup), opponent=AGENTS[spec]() if isinstance(spec, str) else None,
                  seat=seat, engine=engine)
        return {"seed": seed, "env": env, "obs": env.reset(seed), "k": k, "matchup": matchup, "seat": seat,
                "opponent": spec if isinstance(spec, str) else "snapshot", "steps": []}

    while queue or live:
        while queue and len(live) < parallel:
            g = start(queue.pop(0))
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
    return sorted(finished, key=lambda g: g["seed"])


def _rollouts(args):
    """Play games with the learner sampling its moves; returns one episode per game."""
    pairings, state, hidden, opponents, seeds, engine, parallel = args
    games = _play_batched(pairings, seeds, _net(state, hidden), True, opponents, True, hidden, engine, parallel,
                          record=True)
    return [{"steps": g["steps"], "reward": g["env"].reward(), "matchup": g["matchup"], "seat": g["seat"],
             "opponent": g["opponent"]} for g in games]


def _eval_games(args):
    """Returns {(matchup, deck a played): [wins, draws, games]} so a league can be broken down.
    Networks play their best-scored move."""
    pairings, a_spec, b_spec, hidden, seeds, engine, parallel = args
    out: dict = {}
    if isinstance(a_spec, str):  # no network to batch: play the games one by one
        a, b = AGENTS[a_spec](), AGENTS[b_spec]() if isinstance(b_spec, str) else NeuralAgent(_net(b_spec, hidden))
        play = play_rust if engine == "rust" else play_game
        results = []
        for seed in seeds:
            matchup, seat = _assign(pairings, seed)
            results.append((matchup, seat, play(_decks(matchup), (a, b) if seat == 0 else (b, a), seed=seed).winner))
    else:
        games = _play_batched(pairings, seeds, _net(a_spec, hidden), False, [b_spec], False, hidden, engine,
                              parallel, record=False)
        results = [(g["matchup"], g["seat"], g["env"].game.winner) for g in games]
    for matchup, seat, winner in results:
        row = out.setdefault(matchup[seat], [0, 0, 0])
        row[0] += winner == seat
        row[1] += winner is None
        row[2] += 1
    return out


def _device(name: str) -> torch.device:
    if name == "auto":
        name = "mps" if torch.backends.mps.is_available() else "cuda" if torch.cuda.is_available() else "cpu"
    return torch.device(name)


def _cpu_state(net) -> dict:
    """A copy of the weights for the worker processes, which play on the CPU."""
    return {k: v.detach().cpu().clone() for k, v in net.state_dict().items()}


def _to(batch: dict, device) -> dict:
    return {k: v.to(device) for k, v in batch.items()}


def _split(seeds, n):
    return [seeds[i::n] for i in range(n) if seeds[i::n]]


def _opponents(snapshots: list, keep: int, greedy_share: float) -> list:
    """The opponent pool, one entry picked per game: the last `keep` snapshots of the learner,
    plus GreedyAgent repeated so it gets about `greedy_share` of the games."""
    pool = snapshots[-keep:]
    if not pool:
        return ["greedy"]
    n = round(greedy_share / (1 - greedy_share) * len(pool))
    return ["greedy"] * max(int(greedy_share > 0), n) + pool


def _pool(workers):
    return mp.get_context("spawn").Pool(workers, initializer=_init_worker)


# ---------------------------------------------------------------- commands

def evaluate(pool, workers, pairings, a, b, hidden, games, engine="python", parallel=16, seed0=10_000):
    """Share of games `a` wins against `b` (and draws), alternating seats and matchups.
    Also returns the win rate per deck `a` played, which is what a league run wants to see."""
    parts = pool.map(_eval_games,
                     [(pairings, a, b, hidden, s, engine, parallel)
                      for s in _split(list(range(seed0, seed0 + games)), workers)])
    by_deck: dict = {}
    for part in parts:
        for deck, (w, d, n) in part.items():
            row = by_deck.setdefault(deck, [0, 0, 0])
            row[0] += w
            row[1] += d
            row[2] += n
    total = [sum(r[i] for r in by_deck.values()) for i in range(3)]
    return total[0] / games, total[1] / games, {k: (w / n, n) for k, (w, _, n) in by_deck.items()}


def _report(by_deck: dict, indent: str = "    ") -> None:
    for deck, (rate, n) in sorted(by_deck.items(), key=lambda kv: kv[1][0]):
        print(f"{indent}as {deck.split(') ')[-1]:18} {rate:5.1%} ({n})")


def imitate(args) -> None:
    pairings = matchups(args)
    device = _device(args.device)
    net = PolicyNet(args.hidden).to(device)
    opt = torch.optim.Adam(net.parameters(), lr=args.lr)
    with _pool(args.workers) as pool:
        t = time.time()
        data = [x for part in pool.map(_imitation_games,
                                       [(pairings, s, args.engine)
                                        for s in _split(list(range(args.games)), args.workers)])
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
            win, _, by_deck = evaluate(pool, args.workers, pairings, state, vs, args.hidden, args.eval_games,
                                       args.engine, args.parallel)
            print(f"vs {vs}: wins {win:.1%}")
            if len(pairings) > 1:
                _report(by_deck)
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
    if not 0 <= args.greedy_share < 1:
        raise SystemExit("--greedy-share must be at least 0 and below 1")
    pairings = matchups(args)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if len(pairings) > 1:
        print(f"league of {len(pairings)} matchups: {', '.join(_name(m) for m in pairings)}")
    device = _device(args.device)
    net = (load(args.init) if args.init else PolicyNet(args.hidden)).to(device)
    hidden = net.hidden
    opt = torch.optim.Adam(net.parameters(), lr=args.lr)
    snapshots: list[dict] = [_cpu_state(net)] if args.init else []  # self-play from the start
    reference = None
    if args.reference:
        ref = load(args.reference)
        if ref.hidden != hidden:
            raise SystemExit(f"--reference has hidden size {ref.hidden}, the learner {hidden}")
        reference = ref.state_dict()
    best = -1.0
    seed = args.seed
    with _pool(args.workers) as pool:
        for it in range(1, args.iters + 1):
            t = time.time()
            state = _cpu_state(net)
            opponents = _opponents(snapshots, args.keep, args.greedy_share)
            seeds = list(range(seed, seed + args.games))
            seed += args.games
            episodes = [e for part in pool.map(_rollouts, [(pairings, state, hidden, opponents, s, args.engine,
                                                            args.parallel)
                                                           for s in _split(seeds, args.workers)]) for e in part]
            t_play = time.time() - t

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
                by_opp.setdefault(ep["opponent"], []).append(ep["reward"])
            rates = ", ".join(f"vs {k} {sum(r > 0 for r in v) / len(v):.0%} ({len(v)})" for k, v in sorted(by_opp.items()))
            print(f"iter {it}: {len(obs)} decisions, play {t_play:.0f}s, learn {time.time() - t - t_play:.0f}s | "
                  f"{rates} | value loss {value_loss.item():.3f}, entropy {entropy.item():.2f}", flush=True)

            save(net, str(out / "latest.pt"), matchups=pairings, iteration=it)
            if it % args.snapshot_every == 0:
                snapshots.append(_cpu_state(net))
            if it % args.eval_every == 0 or it == args.iters:
                win, draw, by_deck = evaluate(pool, args.workers, pairings, _cpu_state(net), "greedy",
                                              hidden, args.eval_games, args.engine, args.parallel)
                print(f"  eval vs greedy (best move, {args.eval_games} games): wins {win:.1%}, draws {draw:.1%}",
                      flush=True)
                if len(pairings) > 1:
                    _report(by_deck, indent="      ")
                meta = dict(matchups=pairings, iteration=it, eval_vs_greedy=win)
                score = win
                if reference is not None:
                    score, draw, by_deck = evaluate(pool, args.workers, pairings, _cpu_state(net), reference,
                                                    hidden, args.eval_games, args.engine, args.parallel)
                    print(f"  eval vs {args.reference} ({args.eval_games} games): wins {score:.1%}, "
                          f"draws {draw:.1%}", flush=True)
                    if len(pairings) > 1:
                        _report(by_deck, indent="      ")
                    meta["eval_vs_reference"] = score
                save(net, str(out / f"iter{it:04d}.pt"), **meta)
                if score > best:
                    best = score
                    save(net, str(out / "best.pt"), **meta)
                    print(f"  new best: {out / 'best.pt'} (iteration {it})", flush=True)


def eval_cmd(args) -> None:
    pairings = matchups(args)
    net = load(args.checkpoint)
    vs = args.vs if args.vs in AGENTS else load(args.vs).state_dict()
    with _pool(args.workers) as pool:
        win, draw, by_deck = evaluate(pool, args.workers, pairings, net.state_dict(), vs, net.hidden, args.games,
                                      args.engine, args.parallel)
    print(f"{args.checkpoint} vs {args.vs}: wins {win:.1%}, draws {draw:.1%} over {args.games} games")
    if len(pairings) > 1:
        _report(by_deck)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    def common(p):
        p.add_argument("--deck0", default=DECKS[0])
        p.add_argument("--deck1", default=DECKS[1])
        p.add_argument("--league", action="store_true",
                       help="use every pairing of the fully implemented decks instead of --deck0/--deck1")
        p.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
        p.add_argument("--device", default="auto", help="where learning runs: auto, cpu, mps or cuda")
        p.add_argument("--engine", default="auto", choices=("auto", "python", "rust"),
                       help="rules engine for the games: auto uses Rust (optcg_rs) when it is built")
        p.add_argument("--parallel", type=int, default=16,
                       help="games each worker plays at once, so the network scores their moves in one batch")

    p = sub.add_parser("imitate")
    common(p)
    p.add_argument("--out", default="runs/imitate.pt")
    p.add_argument("--games", type=int, default=2000)
    p.add_argument("--epochs", type=int, default=5)
    p.add_argument("--batch", type=int, default=256)
    p.add_argument("--lr", type=float, default=1e-3)
    p.add_argument("--hidden", type=int, default=128)
    p.add_argument("--eval-games", type=int, default=200)
    p.set_defaults(fn=imitate)

    p = sub.add_parser("ppo")
    common(p)
    p.add_argument("--out", default="runs/ppo")
    p.add_argument("--init", help="start from this checkpoint (e.g. the imitation one)")
    p.add_argument("--iters", type=int, default=100)
    p.add_argument("--games", type=int, default=128, help="games per iteration")
    p.add_argument("--epochs", type=int, default=3)
    p.add_argument("--batch", type=int, default=512)
    p.add_argument("--lr", type=float, default=3e-4)
    p.add_argument("--hidden", type=int, default=128)
    p.add_argument("--clip", type=float, default=0.2)
    p.add_argument("--lam", type=float, default=1.0,
                   help="GAE lambda; 1 = plain win/loss returns, lower trusts the value head more")
    p.add_argument("--entropy", type=float, default=0.01)
    p.add_argument("--snapshot-every", type=int, default=10, help="add a frozen copy to the opponent pool")
    p.add_argument("--keep", type=int, default=20, help="how many recent snapshots stay in the pool")
    p.add_argument("--greedy-share", type=float, default=0.25,
                   help="share of games against GreedyAgent; the rest are against snapshots")
    p.add_argument("--reference", help="checkpoint to judge evaluations against head-to-head; best.pt keeps "
                                       "the best (without it, the best against GreedyAgent)")
    p.add_argument("--eval-every", type=int, default=10)
    p.add_argument("--eval-games", type=int, default=200)
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(fn=ppo)

    p = sub.add_parser("eval")
    common(p)
    p.add_argument("checkpoint")
    p.add_argument("--vs", default="greedy", help="random, greedy, or another checkpoint")
    p.add_argument("--games", type=int, default=400)
    p.set_defaults(fn=eval_cmd)

    args = ap.parse_args(argv)
    if args.engine == "auto":
        args.engine = "rust" if rust_available() else "python"
    print(f"engine: {args.engine}", flush=True)
    args.fn(args)


if __name__ == "__main__":
    main()
