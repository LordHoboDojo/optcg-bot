"""An example bot for the bot API (docs/bot-api.md): plays one match, picking options at random.

    OPTCG_KEY=optcg_... .venv/bin/python tools/bot_client.py --url ws://127.0.0.1:8000/api/bot/v1/play
    OPTCG_KEY=optcg_... .venv/bin/python tools/bot_client.py --url wss://optcg.example.com/api/bot/v1/play \\
        --deck rocks --opponent greedy

Replace choose() with your own bot. Needs the `websockets` package (pip install websockets).
"""
from __future__ import annotations

import argparse
import json
import os
import random

from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect


def choose(msg: dict, rng: random.Random) -> int:
    """The option to answer a decision message with: an index into msg["options"]."""
    return rng.randrange(len(msg["options"]))


def play(url: str, key: str, start: dict, seed=None, verbose: bool = False) -> dict:
    """Play one match; returns the server's "over" message."""
    rng = random.Random(seed)
    with connect(url, additional_headers={"Authorization": f"Bearer {key}"}) as ws:
        try:
            ws.send(json.dumps({"type": "start", **start}))
        except ConnectionClosed:  # refused at connect (a bad key, too many matches); the error is still readable
            pass
        while True:
            try:
                msg = json.loads(ws.recv())
            except ConnectionClosed:  # the server refused the start (the error says why)
                return {}
            if msg["type"] == "match":
                print(f"match {msg['match']}: seat {msg['seat']} against {msg['opponent']}")
            elif msg["type"] == "decision":
                option = choose(msg, rng)
                if verbose:
                    print(f"  {msg['prompt']} -> {msg['options'][option]}")
                ws.send(json.dumps({"type": "move", "decision": msg["decision"], "option": option}))
            elif msg["type"] == "error":
                print(f"error {msg['error']}: {msg['detail']}")
            elif msg["type"] == "over":
                print(f"{msg['result']} ({msg['reason']}) in {msg['turns']} turns; replay at {msg['replay']}")
                return msg


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="ws://127.0.0.1:8000/api/bot/v1/play")
    ap.add_argument("--key", default=os.environ.get("OPTCG_KEY"), help="default: $OPTCG_KEY")
    ap.add_argument("--deck", default="rocks", help="a deck name from /decks, or a decklist file")
    ap.add_argument("--opponent", help="a house bot from /decks")
    ap.add_argument("--opponent-deck")
    ap.add_argument("--seat", type=int, choices=(0, 1))
    ap.add_argument("--seed", type=int)
    ap.add_argument("-v", "--verbose", action="store_true", help="print each decision")
    args = ap.parse_args(argv)
    if not args.key:
        ap.error("pass --key or set OPTCG_KEY")
    deck = open(args.deck).read() if os.path.isfile(args.deck) else args.deck
    start = {"deck": deck, **{k: v for k, v in (("opponent", args.opponent), ("opponent_deck", args.opponent_deck),
                                                ("seat", args.seat)) if v is not None}}
    play(args.url, args.key, start, args.seed, args.verbose)


if __name__ == "__main__":
    main()
