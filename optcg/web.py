"""Play against a bot in your browser, on the replay viewer's board.

    .venv/bin/python -m optcg.web --open
    .venv/bin/python -m optcg.web --deck0 "(Black) Monkey.D.Luffy" --deck1 "(Blue) Rocks.D.Xebec" --bot runs/league/latest.pt --open

A small local server runs the game. The page shows only what your seat may see: the bot's hand
stays face down and, of its decisions, only the option it picked. Every step of the game is kept,
so you can scrub back through it while you play.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import random
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, urlparse

from .deck import Deck
from .engine import Game
from .play import MAX_STEPS, make_agent, resolve_deck
from .replay import ASSETS, _def, _frame, art, page

# Decisions whose chosen option would show a hidden card's place, e.g. the order of cards put
# on the bottom of the deck.
SECRET_KINDS = {"order", "order_cards"}


class Session:
    """One game between a person in seat `human` and a bot in the other seat."""

    def __init__(self, decks: tuple[Deck, Deck], bot: str, human: int = 0, seed: Optional[int] = None):
        self.decks, self.bot_spec, self.human = decks, bot, human
        self.seed = random.randrange(1 << 30) if seed is None else seed
        self.bot = make_agent(bot)
        self.g = Game(decks, seed=self.seed)
        self.frames: list[dict] = []
        self.seen = 0
        self._advance()

    def data(self) -> dict:
        """What the page starts from. Frames come from state()."""
        g = self.g
        ids = sorted({c.d.id for c in g.cards.values()})
        out = {"players": [{"leader": self.decks[p].leader.name, "id": self.decks[p].leader.id,
                            "agent": "you" if p == self.human else self.bot_spec} for p in (0, 1)],
               "seed": self.seed, "winner": None, "turns": 0, "first": None,
               "cards": {i: _def(g, i) for i in ids}, "frames": [], "live": {"human": self.human}}
        if a := art(out):
            out["art"] = {**a, "base": "art"}
        return out

    def state(self, since: int = 0) -> dict:
        g = self.g
        return {"from": since, "frames": self.frames[since:], "over": g.over, "winner": g.winner,
                "turns": g.turn_number, "first": g.first_player}

    def act(self, frame: int, option: int) -> None:
        g = self.g
        if g.over or frame != len(self.frames) - 1:
            raise ValueError("that decision has already been made")
        d = g.pending
        if not 0 <= option < len(d.options):
            raise ValueError("no such option")
        a = d.options[option]
        self.frames[frame]["d"].update(pick=option, hl=[u for u in (a.card, a.target) if u is not None])
        g.step(a)
        self._advance()

    def concede(self) -> None:
        if not self.g.over:
            self.g.concede(self.human)
            self.frames.append(self._frame())

    def _frame(self) -> dict:
        f = _frame(self.g, self.seen)
        self.seen = len(self.g.log)
        theirs = f["s"][1 - self.human]
        theirs["H"] = [{} for _ in theirs["H"]]  # face down: count only
        return f

    def _visible(self, f: dict) -> set[int]:
        mine = f["s"][self.human]["H"]
        return {c["u"] for s in f["s"] for c in [s["L"], s["S"], *s["C"]] if c} | {c["u"] for c in mine}

    def _advance(self) -> None:
        """Let the bot play until it's the person's turn to decide, or the game ends."""
        g = self.g
        for _ in range(MAX_STEPS):
            if g.over:
                self.frames.append(self._frame())
                return
            d = g.pending
            f = self._frame()
            base = {"pl": d.player, "kind": d.kind, "q": d.prompt}
            if d.player == self.human:
                f["d"] = {**base, "o": [str(o) for o in d.options], "pick": None, "hl": [],
                          "act": [[o.kind, o.card, o.target] for o in d.options]}
                self.frames.append(f)
                return
            a = self.bot.act(g, d)
            # Only the pick is shown: the other options would give away the bot's hand.
            # Card ids (uids) are kept only for cards the person can already see.
            shown = self._visible(f)
            f["d"] = {**base, "o": [] if d.kind in SECRET_KINDS else [str(a)], "n": len(d.options),
                      "pick": None if d.kind in SECRET_KINDS else 0,
                      "hl": [u for u in (a.card, a.target) if u is not None and u in shown]}
            self.frames.append(f)
            g.step(a)
        raise RuntimeError(f"game did not finish in {MAX_STEPS} steps")


class Handler(BaseHTTPRequestHandler):
    server: "Server"

    def log_message(self, *args) -> None:  # keep the terminal quiet
        pass

    def _send(self, body: bytes, kind: str, status: int = 200, cache: bool = False) -> None:
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "max-age=86400" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, status: int = 200) -> None:
        self._send(json.dumps(obj, separators=(",", ":")).encode(), "application/json", status)

    def do_GET(self) -> None:
        url = urlparse(self.path)
        srv = self.server
        if url.path == "/":
            with srv.lock:
                html = page(srv.session.data())
            self._send(html.encode(), "text/html; charset=utf-8")
        elif url.path == "/api/state":
            since = int(parse_qs(url.query).get("from", ["0"])[0])
            with srv.lock:
                self._json(srv.session.state(since))
        elif url.path.startswith("/art/"):
            f = (ASSETS / url.path[len("/art/"):]).resolve()
            if ASSETS.resolve() in f.parents and f.is_file():
                self._send(f.read_bytes(), mimetypes.guess_type(f.name)[0] or "application/octet-stream", cache=True)
            else:
                self._send(b"not found", "text/plain", 404)
        else:
            self._send(b"not found", "text/plain", 404)

    def do_POST(self) -> None:
        srv = self.server
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        with srv.lock:
            s = srv.session
            try:
                if self.path == "/api/act":
                    s.act(int(body["frame"]), int(body["option"]))
                elif self.path == "/api/concede":
                    s.concede()
                elif self.path == "/api/new":
                    srv.session = Session(s.decks, s.bot_spec, s.human)
                    return self._json({"ok": True})
                else:
                    return self._send(b"not found", "text/plain", 404)
            except (ValueError, KeyError) as e:
                return self._json({"error": str(e), **s.state(int(body.get("frame", 0)))}, 409)
            self._json(s.state(int(body.get("frame", len(s.frames) - 1))))


class Server(ThreadingHTTPServer):
    def __init__(self, addr, session: Session):
        super().__init__(addr, Handler)
        self.session, self.lock = session, threading.Lock()


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--deck0", default="(Blue) Rocks.D.Xebec")
    ap.add_argument("--deck1", default="(Yellow) Charlotte Linlin")
    ap.add_argument("--human", type=int, choices=(0, 1), default=0, help="your seat (default 0, deck0)")
    ap.add_argument("--bot", default="greedy", help="random, greedy or a .pt checkpoint")
    ap.add_argument("--seed", type=int)
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--open", action="store_true", help="open the game in your browser")
    args = ap.parse_args(argv)

    decks = (resolve_deck(args.deck0), resolve_deck(args.deck1))
    srv = Server(("127.0.0.1", args.port), Session(decks, args.bot, args.human, args.seed))
    url = f"http://127.0.0.1:{args.port}/"
    print(f"You: {decks[args.human].leader.name}. Bot ({args.bot}): {decks[1 - args.human].leader.name}.")
    print(f"Playing at {url}  (Ctrl+C to stop)")
    if args.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
