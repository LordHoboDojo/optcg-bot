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

--reference judges the plain network (no search) head-to-head against a fixed checkpoint every
--eval-every iterations, and best.pt keeps the best. Needs the Rust engine.
"""
from __future__ import annotations

import argparse
import os
import random
import time
from collections import deque
from pathlib import Path

import torch
from torch.nn import functional as F

from ..env import encode_rust, rust_available, rust_game
from ..search import MCTSAgent
from .model import collate, load, save
from .train import DECKS, _cpu_state, _decks, _device, _net, _pool, _split, _to, evaluate

MAX_STEPS = 20_000


def _search_games(args):
    """Self-play games with search on both sides. Returns (obs, visit shares, result for the
    player deciding) for every decision that had a choice. The first `explore` of those in a
    game are played by sampling from the visit shares, so games differ; after that, the most
    visited option."""
    state, config, decks, seeds, kw, explore = args
    bot = MCTSAgent(_net(state, config), **kw)
    out = []
    for seed in seeds:
        bot.rng.seed(seed)
        g = rust_game(_decks(decks), seed)
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
        out.extend((obs, visits, result(p)) for obs, visits, p in steps)
    return out


def _learn(net, opt, samples, epochs, batch, device) -> tuple[float, float]:
    """Fit the move scores to the visit shares and the value head to the results."""
    net.train()
    idx = list(range(len(samples)))
    policy_loss = value_loss = torch.tensor(0.0)
    for _ in range(epochs):
        random.shuffle(idx)
        for i in range(0, len(idx), batch):
            mb = [samples[j] for j in idx[i:i + batch]]
            b = _to(collate([s[0] for s in mb]), device)
            logits, value = net(b)
            target = torch.zeros(logits.shape)
            for r, s in enumerate(mb):
                target[r, :len(s[1])] = torch.tensor(s[1])
            logp = torch.log_softmax(logits, -1).masked_fill(~b["option_mask"], 0.0)  # padding is -inf
            policy_loss = -(target.to(device) * logp).sum(-1).mean()
            value_loss = F.mse_loss(value, torch.tensor([s[2] for s in mb], dtype=torch.float32, device=device))
            opt.zero_grad()
            (policy_loss + value_loss).backward()
            torch.nn.utils.clip_grad_norm_(net.parameters(), 1.0)
            opt.step()
    net.eval()
    return policy_loss.item(), value_loss.item()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--init", required=True, help="the checkpoint to start from")
    ap.add_argument("--reference", help="checkpoint to judge the plain network against; best.pt keeps the best")
    ap.add_argument("--out", default="runs/distill")
    ap.add_argument("--deck0", default=DECKS[0])
    ap.add_argument("--deck1", default=DECKS[1])
    ap.add_argument("--iters", type=int, default=40)
    ap.add_argument("--games", type=int, default=64, help="self-play games per iteration")
    ap.add_argument("--sims", type=int, default=100, help="simulations per decision in self-play")
    ap.add_argument("--worlds", type=int, default=16)
    ap.add_argument("--noise", type=float, default=0.25, help="Dirichlet noise mixed into the root priors")
    ap.add_argument("--explore", type=int, default=30,
                    help="decisions per game played by sampling from the visit shares")
    ap.add_argument("--buffer", type=int, default=4, help="iterations of positions to learn from")
    ap.add_argument("--epochs", type=int, default=1)
    ap.add_argument("--batch", type=int, default=512)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--eval-every", type=int, default=2)
    ap.add_argument("--eval-games", type=int, default=400)
    ap.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 1))
    ap.add_argument("--device", default="auto")
    ap.add_argument("--seed", type=int, default=50_000)
    args = ap.parse_args(argv)
    if not rust_available():
        raise SystemExit("training on the search needs the Rust engine (engine-rs/)")

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    device = _device(args.device)
    net = load(args.init).to(device)
    config = net.config()
    opt = torch.optim.Adam(net.parameters(), lr=args.lr)
    reference = load(args.reference).state_dict() if args.reference else None
    pairings = [(args.deck0, args.deck1), (args.deck1, args.deck0)]
    kw = dict(sims=args.sims, worlds=args.worlds, noise=args.noise)
    buffer: deque = deque(maxlen=args.buffer)
    best, seed = -1.0, args.seed
    with _pool(args.workers) as pool:
        for it in range(1, args.iters + 1):
            t = time.time()
            seeds = list(range(seed, seed + args.games))
            seed += args.games
            state = _cpu_state(net)
            parts = pool.map(_search_games, [(state, config, pairings[0], s, kw, args.explore)
                                             for s in _split(seeds, args.workers)])
            buffer.append([x for part in parts for x in part])
            t_play = time.time() - t
            samples = [x for chunk in buffer for x in chunk]
            pl, vl = _learn(net, opt, samples, args.epochs, args.batch, device)
            print(f"iter {it}: {len(buffer[-1])} positions ({len(samples)} in buffer), play {t_play:.0f}s, "
                  f"learn {time.time() - t - t_play:.0f}s | policy loss {pl:.3f}, value loss {vl:.3f}", flush=True)
            save(net, str(out / "latest.pt"), matchups=pairings, iteration=it)

            if reference is not None and (it % args.eval_every == 0 or it == args.iters):
                win, draw, _ = evaluate(pool, args.workers, pairings, _cpu_state(net), reference, config,
                                        args.eval_games, "rust")
                print(f"  eval vs {args.reference} (no search, {args.eval_games} games): wins {win:.1%}, "
                      f"draws {draw:.1%}", flush=True)
                save(net, str(out / f"iter{it:04d}.pt"), matchups=pairings, iteration=it, eval_vs_reference=win)
                if win > best:
                    best = win
                    save(net, str(out / "best.pt"), matchups=pairings, iteration=it, eval_vs_reference=win)
                    print(f"  new best: {out / 'best.pt'} (iteration {it})", flush=True)


if __name__ == "__main__":
    main()
