"""Train and evaluate neural bots. Run with the project venv:

    .venv/bin/python -m optcg.nn.train imitate --out runs/imitate.pt
    .venv/bin/python -m optcg.nn.train ppo --init runs/imitate.pt --out runs/ppo --iters 200
    .venv/bin/python -m optcg.nn.train eval runs/ppo/latest.pt --vs greedy --games 400

imitate  learns to copy GreedyAgent's choices (a fast, sane starting point)
ppo      self-play actor-critic (PPO): the bot plays games against GreedyAgent and its own earlier
         snapshots, and learns from wins and losses. Decks default to Rocks vs Linlin; the bot
         plays both seats, so it learns both decks.
eval     win rate of a checkpoint against an agent (or another checkpoint), both seats

--league replaces the single matchup with every pairing of the decks whose effect text is fully
implemented, sampled per game, so one bot learns them all and far more cards see real play:

    .venv/bin/python -m optcg.nn.train imitate --league --out runs/league_imitate.pt
    .venv/bin/python -m optcg.nn.train ppo --league --init runs/league_imitate.pt --out runs/league
    .venv/bin/python -m optcg.nn.train eval runs/league/latest.pt --league --vs greedy

Games run in parallel worker processes (--workers, default: CPU count - 1); learning runs on
--device (default: the Apple GPU when available, else the CPU).
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
from ..env import Env, encode
from ..play import play_game, resolve_deck
from .model import NeuralAgent, PolicyNet, collate, load, save

DECKS = ("(Blue) Rocks.D.Xebec", "(Yellow) Charlotte Linlin")


def covered_decks() -> tuple[str, ...]:
    """Official decks the engine carries out in full. Anything less would train on missing effects."""
    from ..carddb import coverage, official_decks
    return tuple(sorted(n for n, d in official_decks().items() if coverage(d)[0] == coverage(d)[1]))


def matchups(args) -> tuple[tuple[str, str], ...]:
    """The deck pairings to train on: one matchup, or every pairing with --league."""
    if not getattr(args, "league", False):
        return ((args.deck0, args.deck1),)
    names = covered_decks()
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


def _agent(spec, hidden):
    """'greedy' / 'random', or a network's state dict."""
    return AGENTS[spec]() if isinstance(spec, str) else NeuralAgent(_net(spec, hidden), sample=True)


def _imitation_games(args):
    pairings, seeds = args
    data = []
    for seed in seeds:
        greedy = GreedyAgent()

        class Recorder(GreedyAgent):
            def act(self, g, d):
                a = greedy.act(g, d)
                data.append((encode(g, d), d.options.index(a)))
                return a
        play_game(_decks(_assign(pairings, seed)[0]), (Recorder(), Recorder()), seed=seed)
    return data


def _rollouts(args):
    """Play games with the learner sampling its moves; returns one episode per game."""
    pairings, state, hidden, opponents, seeds = args
    net = _net(state, hidden)
    episodes = []
    for seed in seeds:
        matchup, seat = _assign(pairings, seed)
        opp_spec = random.Random(seed).choice(opponents)
        env = Env(*_decks(matchup), opponent=_agent(opp_spec, hidden), seat=seat)
        obs = env.reset(seed)
        steps = []
        while not env.done:
            with torch.no_grad():
                logits, value = net(collate([obs]))
            dist = torch.distributions.Categorical(logits=logits[0])
            a = dist.sample()
            steps.append((obs, int(a), float(dist.log_prob(a)), float(value[0])))
            obs, reward, _ = env.step(int(a))
        episodes.append({"steps": steps, "reward": env.reward(), "matchup": matchup, "seat": seat,
                         "opponent": opp_spec if isinstance(opp_spec, str) else "snapshot"})
    return episodes


def _eval_games(args):
    """Returns {(matchup, deck a played): [wins, draws, games]} so a league can be broken down."""
    pairings, a_spec, b_spec, hidden, seeds = args
    a = AGENTS[a_spec]() if isinstance(a_spec, str) else NeuralAgent(_net(a_spec, hidden))
    b = AGENTS[b_spec]() if isinstance(b_spec, str) else NeuralAgent(_net(b_spec, hidden))
    out: dict = {}
    for seed in seeds:
        matchup, seat = _assign(pairings, seed)
        g = play_game(_decks(matchup), (a, b) if seat == 0 else (b, a), seed=seed)
        row = out.setdefault(matchup[seat], [0, 0, 0])
        row[0] += g.winner == seat
        row[1] += g.winner is None
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


def _pool(workers):
    return mp.get_context("spawn").Pool(workers, initializer=_init_worker)


# ---------------------------------------------------------------- commands

def evaluate(pool, workers, pairings, a, b, hidden, games, seed0=10_000):
    """Share of games `a` wins against `b` (and draws), alternating seats and matchups.
    Also returns the win rate per deck `a` played, which is what a league run wants to see."""
    parts = pool.map(_eval_games,
                     [(pairings, a, b, hidden, s) for s in _split(list(range(seed0, seed0 + games)), workers)])
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
                                       [(pairings, s) for s in _split(list(range(args.games)), args.workers)])
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
            win, _, by_deck = evaluate(pool, args.workers, pairings, state, vs, args.hidden, args.eval_games)
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
    pairings = matchups(args)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    if len(pairings) > 1:
        print(f"league of {len(pairings)} matchups: {', '.join(_name(m) for m in pairings)}")
    device = _device(args.device)
    net = (load(args.init) if args.init else PolicyNet(args.hidden)).to(device)
    hidden = net.hidden
    opt = torch.optim.Adam(net.parameters(), lr=args.lr)
    snapshots: list[dict] = []
    seed = args.seed
    with _pool(args.workers) as pool:
        for it in range(1, args.iters + 1):
            t = time.time()
            state = _cpu_state(net)
            opponents = ["greedy"] * max(1, len(snapshots)) + snapshots[-args.keep:]
            seeds = list(range(seed, seed + args.games))
            seed += args.games
            episodes = [e for part in pool.map(_rollouts, [(pairings, state, hidden, opponents, s)
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
                                              hidden, args.eval_games)
                print(f"  eval vs greedy (best move, {args.eval_games} games): wins {win:.1%}, draws {draw:.1%}",
                      flush=True)
                if len(pairings) > 1:
                    _report(by_deck, indent="      ")
                save(net, str(out / f"iter{it:04d}.pt"), matchups=pairings, iteration=it, eval_vs_greedy=win)


def eval_cmd(args) -> None:
    pairings = matchups(args)
    net = load(args.checkpoint)
    vs = args.vs if args.vs in AGENTS else load(args.vs).state_dict()
    with _pool(args.workers) as pool:
        win, draw, by_deck = evaluate(pool, args.workers, pairings, net.state_dict(), vs, net.hidden, args.games)
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
    p.add_argument("--keep", type=int, default=5, help="how many recent snapshots stay in the pool")
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
    args.fn(args)


if __name__ == "__main__":
    main()
