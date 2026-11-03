"""The bot API: a program running anywhere plays a match against a house bot over a WebSocket.

The server referees. The remote bot sees only what its seat may see, the same view a person
gets in the browser, and answers each decision with the number of an option it was offered.
A move that isn't one of the offered options ends the match there, as a loss for the bot that
sent it, and so do running out of time, conceding and disconnecting. docs/bot-api.md has the
messages.
"""
from __future__ import annotations

import asyncio
import json
import logging
import secrets
import time
from collections import Counter
from typing import Optional

from starlette.websockets import WebSocket, WebSocketDisconnect, WebSocketState

from ..carddb import DeckError, parse_decklist
from ..deck import Deck
from ..web import Session
from .logs import event

PROTOCOL = 1
START_TIMEOUT = 30.0  # seconds to send the start message after connecting
MAX_MESSAGE = 64 * 1024

# Close codes (4000-4999 are ours to use)
BAD_REQUEST, UNAUTHORIZED, BUSY = 4400, 4401, 4429


class Forfeit(Exception):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class BotMatches:
    """Runs remote bots' matches; `app` is the FastAPI app, for its config, decks and store."""

    def __init__(self, app):
        self.app = app
        self.playing: Counter[int] = Counter()  # bot id -> matches in progress

    async def run(self, ws: WebSocket) -> None:
        await ws.accept()
        st = self.app.state
        auth = ws.headers.get("authorization", "")
        bot = st.store.bot_for_key(auth[len("Bearer "):].strip()) if auth.startswith("Bearer ") else None
        if bot is None:
            return await self._refuse(ws, UNAUTHORIZED, "unauthorized", "send Authorization: Bearer <your API key>")
        if self.playing[bot["id"]] >= st.cfg.bot_matches:
            return await self._refuse(ws, BUSY, "too_many_matches",
                                      f"a bot may play {st.cfg.bot_matches} matches at once")
        self.playing[bot["id"]] += 1
        try:
            await self._match(ws, bot)
        finally:
            self.playing[bot["id"]] -= 1
            if self.playing[bot["id"]] <= 0:
                del self.playing[bot["id"]]

    async def _match(self, ws: WebSocket, bot) -> None:
        st = self.app.state
        try:
            start = await asyncio.wait_for(self._receive(ws), START_TIMEOUT)
        except (asyncio.TimeoutError, WebSocketDisconnect):
            return await self._refuse(ws, BAD_REQUEST, "bad_request", "send a start message after connecting")
        try:
            seat, opponent, decks = self._read_start(start)
        except ValueError as e:
            return await self._refuse(ws, BAD_REQUEST, "bad_request", str(e))

        s: Session = await asyncio.to_thread(Session, decks, st.cfg.house_bots[opponent], seat, None, opponent)
        match_id = secrets.token_hex(8)
        names = [d.leader.name for d in decks]
        st.store.start_match(match_id, bot["id"], opponent, seat, names, s.seed)
        event("match_start", match=match_id, bot=bot["name"], opponent=opponent, seat=seat, decks=names)
        started = time.monotonic()
        data = s.data()
        await self._send(ws, {"type": "match", "protocol": PROTOCOL, "match": match_id, "seat": seat,
                              "opponent": opponent, "players": data["players"], "cards": data["cards"],
                              "timeout": st.cfg.move_timeout})
        sent, reason = 0, "normal"
        try:
            while not s.g.over:
                decision, d = len(s.frames) - 1, s.frames[-1]["d"]
                await self._send(ws, {"type": "decision", "decision": decision, "kind": d["kind"], "prompt": d["q"],
                                      "options": d["o"], "actions": d["act"], "frames": s.frames[sent:]})
                sent = len(s.frames)
                option = await self._move(ws, decision, len(d["o"]), match_id)
                await asyncio.to_thread(s.act, decision, option)
        except Forfeit as f:
            reason = f.reason
            s.concede()
        winner = s.g.winner
        result = "draw" if winner is None else "win" if winner == seat else "loss"
        st.store.end_match(match_id, winner, reason, s.g.turn_number)
        self._save_replay(match_id, s)
        event("match_over", match=match_id, bot=bot["name"], opponent=opponent, result=result, reason=reason,
              turns=s.g.turn_number, seconds=round(time.monotonic() - started, 1))
        await self._send(ws, {"type": "over", "match": match_id, "winner": winner, "result": result, "reason": reason,
                              "turns": s.g.turn_number, "frames": s.frames[sent:], "replay": f"/matches/{match_id}"})
        await self._close(ws, 1000)

    def _read_start(self, msg) -> tuple[int, str, tuple[Deck, Deck]]:
        st = self.app.state
        if not isinstance(msg, dict) or msg.get("type") != "start":
            raise ValueError('the first message is {"type": "start", "deck": ...}')
        seat = msg.get("seat", secrets.randbelow(2))
        if seat not in (0, 1) or type(seat) is not int:
            raise ValueError("seat is 0 or 1")
        bots = st.cfg.house_bots
        opponent = msg.get("opponent", next(iter(bots)))
        if opponent not in bots:
            raise ValueError(f"opponent is one of {list(bots)}")
        mine, theirs = self._deck(msg.get("deck")), self._deck(msg.get("opponent_deck", st.house_deck))
        return seat, opponent, (mine, theirs) if seat == 0 else (theirs, mine)

    def _deck(self, spec) -> Deck:
        """A deck the server offers, by name, or a decklist ('4xOP01-016' per line)."""
        decks = self.app.state.decks
        if not isinstance(spec, str) or not spec.strip():
            raise ValueError("deck is a deck name from /decks or a decklist")
        if spec in decks:
            return decks[spec]
        if "\n" not in spec.strip() and not any(c.isdigit() for c in spec[:3]):
            raise ValueError(f"no deck named {spec!r}; see /decks")
        try:
            deck = parse_decklist(spec)
            deck.validate()
        except (DeckError, KeyError) as e:
            raise ValueError(f"bad decklist: {e}") from None
        return deck

    async def _move(self, ws: WebSocket, decision: int, n: int, match_id: str) -> int:
        """Wait for the answer to `decision`. Messages that aren't a move for it are answered
        with an error and don't stop the clock."""
        deadline = time.monotonic() + self.app.state.cfg.move_timeout
        while True:
            left = deadline - time.monotonic()
            try:
                if left <= 0:
                    raise asyncio.TimeoutError
                msg = await asyncio.wait_for(self._receive(ws), left)
            except asyncio.TimeoutError:
                await self._error(ws, "timeout", f"no move within {self.app.state.cfg.move_timeout:g} seconds")
                raise Forfeit("timeout") from None
            except WebSocketDisconnect:
                raise Forfeit("disconnect") from None
            except ValueError as e:
                await self._error(ws, "bad_request", str(e))
                continue
            kind = msg.get("type") if isinstance(msg, dict) else None
            if kind == "concede":
                raise Forfeit("concede")
            if kind != "move" or type(msg.get("decision")) is not int:
                await self._error(ws, "bad_request", 'send {"type": "move", "decision": <number>, "option": <number>}')
                continue
            if msg["decision"] != decision:  # a retry of an answered decision, or a guess at a later one
                await self._error(ws, "stale_decision", f"the decision waiting is {decision}", decision=decision)
                continue
            option = msg.get("option")
            if type(option) is not int or not 0 <= option < n:
                event("illegal_move", logging.WARNING, match=match_id, decision=decision, option=option, options=n)
                await self._error(ws, "illegal_move", f"option {option!r} was not offered (0 to {n - 1}); "
                                                      "the match is over", decision=decision)
                raise Forfeit("illegal_move")
            return option

    def _save_replay(self, match_id: str, s: Session) -> None:
        """The match from the remote bot's seat, for /matches/<id>."""
        g = s.g
        rep = {**s.data(), "frames": s.frames, "winner": g.winner, "turns": g.turn_number, "first": g.first_player}
        rep.pop("live", None)
        rep.pop("art", None)
        folder = self.app.state.cfg.replays
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{match_id}.json").write_text(json.dumps(rep, separators=(",", ":")))

    @staticmethod
    async def _receive(ws: WebSocket):
        """The next message as JSON. ValueError if it isn't JSON; WebSocketDisconnect if the bot left."""
        m = await ws.receive()
        if m["type"] == "websocket.disconnect":
            raise WebSocketDisconnect(m.get("code", 1000))
        text = m.get("text")
        if text is None or len(text) > MAX_MESSAGE:
            raise ValueError("send JSON text messages under 64 KB")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise ValueError("that message isn't JSON") from None

    @staticmethod
    async def _send(ws: WebSocket, msg: dict) -> None:
        if ws.client_state == WebSocketState.CONNECTED:
            try:
                await ws.send_text(json.dumps(msg, separators=(",", ":")))
            except (WebSocketDisconnect, RuntimeError):
                pass

    async def _error(self, ws: WebSocket, code: str, detail: str, **extra) -> None:
        await self._send(ws, {"type": "error", "error": code, "detail": detail, **extra})

    async def _refuse(self, ws: WebSocket, close: int, code: str, detail: str) -> None:
        await self._error(ws, code, detail)
        await self._close(ws, close)

    @staticmethod
    async def _close(ws: WebSocket, code: int) -> None:
        if ws.client_state == WebSocketState.CONNECTED:
            try:
                await ws.close(code)
            except RuntimeError:
                pass
