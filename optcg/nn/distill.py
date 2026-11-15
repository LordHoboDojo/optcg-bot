"""Train the network on the search's choices (expert iteration, as in AlphaZero).

Each iteration the network plays games against itself with search on both sides
(optcg/search.py). At every decision with a choice, the position is kept with the share of
the search's visits each option got, and later with how the game ended. The network then learns
to score options the way the search ended up rating them (cross-entropy against the visit
shares) and to predict the result (its value head). A better network makes the next round's
search better, which makes the next targets better.

PPO learns from one win or loss per game; here every searched decision carries its own target,
which is a far richer signal once the bot is past the basics. Start from a trained checkpoint:

    .venv/bin/python -m optcg.nn.distill --init runs/ppo/latest.pt --reference runs/ppo/latest.pt --out runs/distill

--decks FILE trains on a league of decks (phase 3 of the league plan, after PPO levels off).
Each game's matchup is drawn with more weight on the matchups where the plain network's edge
over greedy is smallest (league.MatchupSampler, fed by the evaluations' matchup tables;
--targets seeds it with an earlier table). Every --eval-every iterations the plain network (no
search) plays every matchup against greedy, and with --reference head-to-head against that
checkpoint across the league (each deal played both ways round, so equal networks tie exactly):

    .venv/bin/python -m optcg.nn.distill --decks decks/league15.txt --init runs/league15_lr/best.pt \\
        --reference runs/league15_lr/best.pt --targets runs/league15_p2/final_p1best.json --out runs/league15_p3

best.pt keeps the iteration that did best against --reference (else, with --decks, the one with
the best mean edge over greedy), starting with --init itself, so best.pt never ends up worse than
where the run started. --resume continues the run in --out from latest.pt and its optimizer state
(the replay buffer starts empty again). Needs the Rust engine.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import random
import time
from collections import deque
from pathlib import Path

import torch
from torch.nn import functional as F

from ..env import encode_rust, rust_available, rust_game
from ..search import MCTSAgent
from . import league
from .model import collate, load, save
from .train import (DECKS, _cpu_state, _decks, _device, _eval_games, _net, _pool, _print_table, _report, _split, _to,
                    _to_cpu, evaluate, greedy_baseline, matchups, table_eval)

MAX_STEPS = 20_000


def _search_games(args):
    """Self-play games with search on both sides, one per job (seed, decks in seat order).
    Returns (samples, games): a sample (obs, visit shares, result for the player deciding) for
    every decision that had a choice, and (decks, winner, decisions) per game. The first
    `explore` of a game's decisions are played by sampling from the visit shares, so games differ;
    after that, the most visited option."""
    state, config, jobs, kw, explore = args
    torch.set_num_threads(1)
    bot = MCTSAgent(_net(state, config), **kw)
    samples, games = [], []
    for seed, decks in jobs:
        bot.rng.seed(seed)
        g = rust_game(_decks(tuple(decks)), seed)
        steps = []
        for _ in range(MAX_STEPS):
            if g.over:
                break
            if g.num_options() == 1:
                g.step(0)
                continue
            obs = encode_rust(g)
            visits, prior = bot.policy(g)
            steps.append((obs, visits, g.pending_player()))
            if len(steps) <= explore:
                i = bot.rng.choices(range(len(visits)), visits)[0]
            else:
                i = max(range(len(visits)), key=lambda j: (visits[j], prior[j]))
            g.step(i)
        result = lambda p: 0.0 if g.winner is None else (1.0 if g.winner == p else -1.0)
        samples.extend((obs, visits, result(p)) for obs, visits, p in steps)
        games.append((tuple(decks), g.winner, len(steps)))
    return samples, games


def _learn(net, opt, samples, epochs, batch, device, value_weight=1.0) -> dict:
    """Fit the move scores to the visit shares and the value head to the results. Returns the
    mean policy loss, its KL part (the loss minus the targets' own entropy, 0 when the network
    already scores options as the search does) and the mean value loss."""
    net.train()
    idx = list(range(len(samples)))
    totals = {"policy": 0.0, "kl": 0.0, "value": 0.0}
    n = 0
    for _ in range(epochs):
        random.shuffle(idx)
        for i in range(0, len(idx), batch):
            mb = [samples[j] for j in idx[i:i + batch]]
            b = _to(collate([s[0] for s in mb]), device)
            logits, value = net(b)
            target = torch.zeros(logits.shape)
            for r, s in enumerate(mb):
                target[r, :len(s[1])] = torch.tensor(s[1])
            target = target.to(device)
            logp = torch.log_softmax(logits, -1).masked_fill(~b["option_mask"], 0.0)  # padding is -inf
            policy_loss = -(target * logp).sum(-1).mean()
            entropy = -(target * torch.log(target.clamp_min(1e-12))).sum(-1).mean()
            value_loss = F.mse_loss(value, torch.tensor([s[2] for s in mb], dtype=torch.float32, device=device))
            opt.zero_grad()
            (policy_loss + value_weight * value_loss).backward()
            torch.nn.utils.clip_grad_norm_(net.parameters(), 1.0)
            opt.step()
            k = len(mb)
            totals["policy"] += policy_loss.item() * k
            totals["kl"] += (policy_loss - entropy).item() * k
            totals["value"] += value_loss.item() * k
            n += k
    net.eval()
    return {key: v / max(n, 1) for key, v in totals.items()}


def game_jobs(rng: random.Random, n: int, sampler: league.MatchupSampler, seed0: int) -> list[tuple]:
    """`n` self-play games: (seed, decks in seat order). Each matchup comes from the sampler and
    its first deck takes a random seat."""
    out = []
    for i, (a, b) in enumerate(sampler.sample(rng, n)):
        out.append((seed0 + i, (a, b) if rng.random() < 0.5 else (b, a)))
    return out


def _feed(sampler: league.MatchupSampler, t: dict, per: int, pairings) -> None:
    """Give the sampler a matchup table's results against greedy, replacing the ones it had."""
    sampler.reset("greedy")
    for a, row in zip(t["decks"], t["win"]):
        for b, w in zip(t["decks"], row):
            if w is not None and (a, b) in pairings:
                sampler.record("greedy", (a, b), w * per, per)


def paired_jobs(pairings, games: int, seed0: int) -> list[tuple]:
    """Head-to-head jobs in deal pairs: matchups in turn (shuffled order, fixed by seed0), and each
    deal played twice with the same seed and seats, `a` holding one deck and then the other. Equal
    networks (best move, deterministic) then score exactly 1/2, and deck strength and lucky deals
    cancel. Jobs are train.py's (seed, a's deck, b's deck, a's seat, "b")."""
    order = list(pairings)
    random.Random(seed0).shuffle(order)
    m, jobs = len(order), []
    for i in range(max(1, games // 2)):
        x, y = order[i % m]
        seat = (i // m) % 2
        jobs += [(seed0 + i, x, y, seat, "b"), (seed0 + i, y, x, 1 - seat, "b")]
    return jobs


def head_to_head(pool, workers, pairings, a, b, config, games, parallel=16, seed0=30_000):
    """`a`'s score against `b` over paired deals (paired_jobs), and its score per deck it played."""
    jobs = paired_jobs(pairings, games, seed0)
    results = [r for part in pool.map(_eval_games, [(j, a, b, config, "rust", parallel)
                                                    for j in _split(jobs, workers)]) for r in part]
    by_deck: dict = {}
    for (mine, _), _, score in results:
        row = by_deck.setdefault(mine, [0.0, 0])
        row[0] += score
        row[1] += 1
    return sum(r[2] for r in results) / len(results), {k: (w / n, n) for k, (w, n) in by_deck.items()}, len(results)


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--init", help="the checkpoint to start from (not needed with --resume)")
    ap.add_argument("--resume", action="store_true",
                    help="continue the run in --out from its latest.pt and optimizer state; --iters is the total")
    ap.add_argument("--reference", help="checkpoint to judge the plain network against head-to-head; best.pt keeps "
                                        "the best")
    ap.add_argument("--out", default="runs/distill")
    ap.add_argument("--deck0", default=DECKS[0])
    ap.add_argument("--deck1", default=DECKS[1])
    ap.add_argument("--decks", help="a deck file (e.g. decks/league15.txt): train on every matchup of a league")
    ap.add_argument("--iters", type=int, default=40)
    ap.add_argument("--games", type=int, default=64, help="self-play games per iteration")
    ap.add_argument("--sims", type=int, default=100, help="simulations per decision in self-play")
    ap.add_argument("--worlds", type=int, default=16)
    ap.add_argument("--c-puct", type=float, default=1.5)
    ap.add_argument("--noise", type=float, default=0.0,
                    help="Dirichlet noise mixed into the root priors. It ends up in the visit shares the network "
                         "learns (with a network the search mostly agrees with, 0.25 took a 15-deck run from +10%% "
                         "to +1%% over greedy in two iterations), so games vary through --explore instead")
    ap.add_argument("--explore", type=int, default=30,
                    help="decisions per game played by sampling from the visit shares")
    ap.add_argument("--buffer", type=int, default=4, help="iterations of positions to learn from")
    ap.add_argument("--epochs", type=int, default=1)
    ap.add_argument("--batch", type=int, default=512)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--value-weight", type=float, default=1.0, help="weight of the value loss")
    ap.add_argument("--eval-every", type=int, default=2)
    ap.add_argument("--eval-games", type=int, default=400,
                    help="head-to-head games against --reference (deal pairs, each played both ways round)")
    ap.add_argument("--table-games", type=int, default=8,
                    help="with --decks: games per matchup in each evaluation against greedy")
    ap.add_argument("--baseline-games", type=int, default=32,
                    help="with --decks: greedy-vs-greedy games per matchup for the baseline (cached)")
    ap.add_argument("--targets", help="with --decks: a matchup table JSON (e.g. a final eval of --init) the "
                                      "matchup sampler starts from")
    ap.add_argument("--matchup-floor", type=float, default=0.5,
                    help="share of games spread evenly over every matchup; the rest favour the smallest edges")
    ap.add_argument("--matchup-temp", type=float, default=0.1)
    ap.add_argument("--parallel", type=int, default=16, help="evaluation games per worker played at once")
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--device", default="auto")
    ap.add_argument("--seed", type=int, default=50_000)
    args = ap.parse_args(argv)
    args.engine = "rust"
    if not rust_available():
        raise SystemExit("training on the search needs the Rust engine (engine-rs/)")
    if not args.init and not args.resume:
        raise SystemExit("--init is required (or --resume to continue a run)")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    device = _device(args.device)
    tables = bool(args.decks)
    decks = league.read_decks(args.decks) if tables else [args.deck0, args.deck1]
    pairings = matchups(args)
    key = "eval_vs_reference" if args.reference else "mean_edge" if tables else "eval_vs_greedy"

    start, best = 0, float("-inf")
    if args.resume:
        if not (out / "latest.pt").exists():
            raise SystemExit(f"--resume: {out / 'latest.pt'} doesn't exist")
        ck = torch.load(out / "latest.pt", map_location="cpu", weights_only=False)
        start = ck.get("iteration", 0)
        if start >= args.iters:
            raise SystemExit(f"--resume: the run is already at iteration {start}; raise --iters")
        net = load(str(out / "latest.pt")).to(device)
        if (out / "best.pt").exists():
            b = torch.load(out / "best.pt", map_location="cpu", weights_only=False)
            if b.get(key) is not None:
                best = b[key]
    else:
        net = load(args.init).to(device)
    config = net.config()
    opt = torch.optim.Adam(net.parameters(), lr=args.lr)
    if args.resume:
        if ck.get("optimizer"):
            opt.load_state_dict(ck["optimizer"])
            for g in opt.param_groups:
                g["lr"] = args.lr
        print(f"resuming {out} after iteration {start}"
              + (" with its saved optimizer state" if ck.get("optimizer") else ""), flush=True)
    reference = load(args.reference).state_dict() if args.reference else None
    sampler = league.MatchupSampler(pairings, floor=args.matchup_floor, temp=args.matchup_temp, decay=1.0)
    kw = dict(sims=args.sims, worlds=args.worlds, noise=args.noise, c_puct=args.c_puct)
    buffer: deque = deque(maxlen=args.buffer)
    rng = random.Random(args.seed + 1_000_003 * start)
    meta = dict(matchups=pairings, decks=decks if tables else None)

    with _pool(args.workers) as pool:
        baseline = None

        def judge(state, it: int, feed: bool = True) -> dict:
            """Evaluate the plain network: its scores by name, with the table saved and (with `feed`)
            given to the matchup sampler."""
            t0, scores = time.time(), {}
            if tables:
                tbl, _ = table_eval(pool, args, decks, state, config, baseline, it)
                print(f"  vs greedy, every matchup ({args.table_games} games each):", flush=True)
                _print_table(tbl, "    ")
                league.save_table(tbl, out / f"table_iter{it:04d}.json")
                league.save_table(tbl, out / "table_latest.json")
                if feed:
                    _feed(sampler, tbl, args.table_games, pairings)
                scores["mean_edge"] = tbl["mean_edge"]
            if reference is not None:
                score, by_deck, n = head_to_head(pool, args.workers, pairings, state, reference, config,
                                                 args.eval_games, args.parallel)
                margin = 1.96 * math.sqrt(max(score * (1 - score), 1e-9) / n)
                print(f"  vs {args.reference} (no search, {n} games, each deal both ways round): score {score:.1%} "
                      f"+/- {margin:.1%}", flush=True)
                if tables:
                    _report(by_deck)
                scores["eval_vs_reference"] = score
            if not tables and reference is None:
                win, draw, _ = evaluate(pool, args.workers, pairings, state, "greedy", config, args.eval_games,
                                        "rust", args.parallel)
                print(f"  vs greedy (no search, {args.eval_games} games): wins {win:.1%}", flush=True)
                scores["eval_vs_greedy"] = win
            print(f"  evaluation took {time.time() - t0:.0f}s", flush=True)
            return scores

        seed_table = None
        if tables:
            baseline = greedy_baseline(pool, args.workers, decks, args.baseline_games, "rust", args.parallel,
                                       out / "greedy_baseline.json")
            sampler.set_baseline({k: s / n for k, (s, n) in baseline.items()})
            seed_table = out / "table_latest.json" if args.resume else None
            if seed_table is None or not seed_table.exists():
                seed_table = Path(args.targets) if args.targets else None
            if seed_table is not None:
                t = json.loads(seed_table.read_text(encoding="utf-8"))
                per = t.get("games_per_matchup", args.table_games)
                _feed(sampler, t, per, pairings)
                p = dict(zip(pairings, sampler.probs()))
                worst = sorted(p, key=p.get, reverse=True)[:3]
                print(f"matchup sampler seeded from {seed_table}; most played: "
                      + ", ".join(f"{league.short(a)} v {league.short(b)} {p[(a, b)]:.1%}" for a, b in worst),
                      flush=True)

        if not args.resume:  # the starting checkpoint is the one to beat
            print(f"iteration 0 ({args.init}):", flush=True)
            scores = judge(_cpu_state(net), 0, feed=seed_table is None)  # a --targets table has more games
            best = scores[key]
            save(net, str(out / "best.pt"), iteration=0, **meta, **scores)

        seed = args.seed + start * args.games
        for it in range(start + 1, args.iters + 1):
            t = time.time()
            jobs = game_jobs(rng, args.games, sampler, seed)
            seed += args.games
            state = _cpu_state(net)
            parts = pool.map(_search_games, [(state, config, j, kw, args.explore)
                                             for j in _split(jobs, args.workers)])
            fresh = [x for samples, _ in parts for x in samples]
            games = [x for _, played in parts for x in played]
            buffer.append(fresh)
            t_play = time.time() - t
            samples = [x for chunk in buffer for x in chunk]
            losses = _learn(net, opt, samples, args.epochs, args.batch, device, args.value_weight)
            draws = sum(w is None for _, w, _ in games)
            print(f"iter {it}: {len(games)} games, {len(fresh)} positions ({len(samples)} in buffer), "
                  f"{sum(n for *_, n in games) / max(len(games), 1):.0f} decisions/game, {draws} draws, "
                  f"play {t_play:.0f}s, learn {time.time() - t - t_play:.0f}s | policy loss {losses['policy']:.3f} "
                  f"(KL {losses['kl']:.3f}), value loss {losses['value']:.3f}", flush=True)
            save(net, str(out / "latest.pt"), iteration=it, optimizer=_to_cpu(opt.state_dict()), **meta)

            if it % args.eval_every == 0 or it == args.iters:
                scores = judge(_cpu_state(net), it)
                save(net, str(out / f"iter{it:04d}.pt"), iteration=it, **meta, **scores)
                if scores[key] > best:
                    best = scores[key]
                    save(net, str(out / "best.pt"), iteration=it, **meta, **scores)
                    print(f"  new best: {out / 'best.pt'} (iteration {it}, {key} {best:.3f})", flush=True)


if __name__ == "__main__":
    main()
