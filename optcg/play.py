"""Run bot-vs-bot games.

    python -m optcg.play --games 200 --p0 greedy --p1 random
    python -m optcg.play --deck0 "(Red) Roronoa Zoro" --deck1 "Doflamingo" --games 100
    python -m optcg.play --deck0 my_deck.txt --games 1 --log
    python -m optcg.play --list-decks
    python -m optcg.play --missing "(Red) Roronoa Zoro"
    python -m optcg.play --deck0 "(Blue) Rocks.D.Xebec" --deck1 "(Yellow) Charlotte Linlin" --human 0 --p1 runs/ppo/latest.pt

A deck is an official recommended deck name (or a unique part of one), a decklist
file ('4xOP01-016' per line), or sample-red / sample-green.
A bot is random, greedy, or a trained network checkpoint (.pt, needs PyTorch).
"""
from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path

from .agents import AGENTS, Agent
from .cardpool import GREEN_DECK, RED_DECK
from .deck import Deck
from .engine import Game

MAX_STEPS = 20_000
SAMPLE_DECKS = {"sample-red": RED_DECK, "sample-green": GREEN_DECK}


def play_game(decks: tuple[Deck, Deck], agents: tuple[Agent, Agent], seed=None) -> Game:
    game = Game(decks, seed=seed)
    for _ in range(MAX_STEPS):
        if game.over:
            return game
        d = game.pending
        game.step(agents[d.player].act(game, d))
    raise RuntimeError(f"game did not finish in {MAX_STEPS} steps")


def play_human(decks: tuple[Deck, Deck], seat: int, bot: Agent, seed=None, human=None) -> Game:
    from .human import HELP, Concede, HumanAgent
    human = human or HumanAgent()
    agents = [bot, bot]
    agents[seat] = human
    human.out(f"You are player {seat} ({decks[seat].leader.name}) against the bot ({decks[1 - seat].leader.name}).")
    human.out(HELP)
    game = Game(decks, seed=seed)
    try:
        while not game.over:
            d = game.pending
            game.step(agents[d.player].act(game, d))
    except Concede:
        game.concede(seat)
    human.show_new_log(game)
    human.out("\nYou win!" if game.winner == seat else "\nDraw." if game.winner is None else "\nYou lose.")
    return game


def make_agent(spec: str) -> Agent:
    if spec in AGENTS:
        return AGENTS[spec]()
    if spec.endswith(".pt"):
        from .nn.model import NeuralAgent, load
        return NeuralAgent(load(spec))
    raise SystemExit(f"unknown bot {spec!r}: use {', '.join(AGENTS)} or a .pt checkpoint")


def resolve_deck(spec: str) -> Deck:
    if spec in SAMPLE_DECKS:
        return SAMPLE_DECKS[spec]
    from .carddb import find_deck, parse_decklist
    if Path(spec).is_file():
        deck = parse_decklist(Path(spec).read_text())
        deck.validate()
        return deck
    return find_deck(spec)


def list_decks() -> None:
    from .carddb import coverage, official_decks
    rows = []
    for name, deck in official_decks().items():
        done, total, _ = coverage(deck)
        rows.append((done / max(total, 1), name))
    for pct, name in sorted(rows, reverse=True):
        print(f"{pct:4.0%}  {name}")
    print("\n% = share of effect text in the deck the engine carries out")


def show_missing(spec: str) -> None:
    from .carddb import coverage
    done, total, missing = coverage(resolve_deck(spec))
    print(f"{done}/{total} effect lines implemented. Not implemented:")
    for line in sorted(set(missing)):
        print(f"  {missing.count(line)}x {line}")


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--games", type=int, default=100)
    ap.add_argument("--p0", default="greedy", help="bot for player 0: random, greedy or a .pt checkpoint")
    ap.add_argument("--p1", default="random", help="bot for player 1: random, greedy or a .pt checkpoint")
    ap.add_argument("--deck0", default="sample-red")
    ap.add_argument("--deck1", default="sample-green")
    ap.add_argument("--seed", type=int, default=None, help="random seed (default: 0, or random with --human)")
    ap.add_argument("--log", action="store_true", help="print the game log of every game")
    ap.add_argument("--human", type=int, choices=(0, 1), help="play this seat yourself against the other bot")
    ap.add_argument("--list-decks", action="store_true", help="list official decks with effect coverage")
    ap.add_argument("--missing", metavar="DECK", help="list a deck's unimplemented effect text")
    args = ap.parse_args(argv)

    if args.list_decks:
        return list_decks()
    if args.missing:
        return show_missing(args.missing)

    decks = (resolve_deck(args.deck0), resolve_deck(args.deck1))
    if args.human is not None:
        return play_human(decks, args.human, make_agent(args.p1 if args.human == 0 else args.p0), args.seed)
    args.seed = args.seed or 0
    results: Counter = Counter()
    turns = 0
    for i in range(args.games):
        agents = (make_agent(args.p0), make_agent(args.p1))
        g = play_game(decks, agents, seed=args.seed + i)
        results[g.winner] += 1
        turns += g.turn_number
        if args.log:
            print("\n".join(g.log), end="\n\n")
    n = args.games
    names = [d.leader.name for d in decks]
    print(f"{n} games: p0 ({args.p0}, {names[0]}) won {results[0]}, p1 ({args.p1}, {names[1]}) won {results[1]}, "
          f"draws {results[None]}; average {turns / n:.1f} turns")


if __name__ == "__main__":
    main()
