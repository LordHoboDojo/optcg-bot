"""The hosted site: many people playing house bots in the browser, optional Google sign-in, and
the bot API for programs that play over a WebSocket.

The pages are a React app (web/, built to web/dist) that talks to the JSON API under /api.
The game table and replays are the viewer from optcg.replay, served at /game and /matches/<id>.

    create_app(Config.from_env())  # see __main__.py to run it
"""
from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import secrets
import threading
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import Body, FastAPI, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, StrictInt
from starlette.middleware.sessions import SessionMiddleware

from ..carddb import DATA, parse_decklist
from ..deck import Deck, DeckError
from ..play import coverage, make_agent
from ..replay import ASSETS, _art_index, art, art_files, page
from ..web import MAX_BODY, Session
from .botapi import BotMatches
from .config import Config
from .logs import event, log
from .store import Store

ROOT = Path(__file__).resolve().parents[2]
DOCS = ROOT / "docs" / "bot-api.md"
ANALYSIS = ROOT / "data" / "analysis.json"
SITE = ROOT / "web" / "dist"
PAGES = ("", "play", "builder", "account", "bots", "analysis", "docs", "docs/bot-api", "signin")  # the React app's routes


@dataclass
class Room:
    """One visitor's game in the browser."""
    session: Session
    deck: str
    bot_deck: str
    user_id: Optional[int]
    lock: threading.Lock = field(default_factory=threading.Lock)
    last: float = field(default_factory=time.monotonic)
    recorded: bool = False


class Rooms:
    """Live browser games, kept in memory by a token in the visitor's cookie and dropped after
    sitting idle. A restart ends them all."""

    def __init__(self, cap: int, idle_minutes: float):
        self.cap, self.idle = cap, idle_minutes * 60
        self.rooms: dict[str, Room] = {}
        self.lock = threading.Lock()

    def get(self, token: Optional[str]) -> Optional[Room]:
        with self.lock:
            room = self.rooms.get(token or "")
            if room:
                room.last = time.monotonic()
            return room

    def add(self, room: Room) -> Optional[str]:
        """A token for the new game, or None when the server is full."""
        with self.lock:
            now = time.monotonic()
            for t in [t for t, r in self.rooms.items() if now - r.last > self.idle]:
                del self.rooms[t]
            if len(self.rooms) >= self.cap:
                return None
            token = secrets.token_urlsafe(16)
            self.rooms[token] = room
            return token

    def drop(self, token: Optional[str]) -> None:
        with self.lock:
            self.rooms.pop(token or "", None)


class Act(BaseModel):
    frame: StrictInt = Field(ge=0)
    option: StrictInt = Field(ge=0)


class NewGame(BaseModel):
    deck: str = Field(max_length=200)
    bot_deck: str = Field(max_length=200)
    bot: str = Field(max_length=100)
    seat: Optional[StrictInt] = Field(default=None, ge=0, le=1)
    decklist: Optional[str] = Field(default=None, max_length=8000)  # your own deck from the builder; `deck` is then its name


class Named(BaseModel):
    name: str = Field(max_length=64)


def _error(detail: str, status: int = 400, code: str = "bad_request") -> JSONResponse:
    return JSONResponse({"error": code, "detail": detail}, status)


UNLIMITED = "any number of this card in your deck"  # as optcg/deck.py reads it


def _sha(spec: str) -> Optional[str]:
    path = ROOT / spec.removeprefix("mcts:")
    return hashlib.sha256(path.read_bytes()).hexdigest() if spec.endswith(".pt") and path.is_file() else None


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    st = app.state
    st.cfg, st.store = cfg, Store(cfg.db)
    st.decks: dict[str, Deck] = cfg.load_decks()
    st.house_deck = cfg.house_deck if cfg.house_deck in st.decks else next(iter(st.decks))
    st.rooms = Rooms(cfg.max_games, cfg.idle_minutes)
    st.art_files = cfg.art_files()
    files = art_files() if st.art_files is None else st.art_files
    local_art = cfg.art_base == "art" and bool(files)
    art_base = "/art" if cfg.art_base == "art" else cfg.art_base
    bots = cfg.house_bots
    for spec in bots.values():
        make_agent(spec)  # fail at startup, not on the first game, if a house bot can't load
    matches = BotMatches(app)
    analysis = json.loads(ANALYSIS.read_text()) if ANALYSIS.is_file() else None

    # What the Play page shows: each deck's Leader and art, and each house bot's rating if the analysis has it
    index = _art_index(tuple(files)) if files else {}
    deck_list = []
    for name, d in st.decks.items():
        done, total, _ = coverage(d)
        leader = d.leader
        deck_list.append({"name": name, "leader": leader.name, "leader_id": leader.id, "colors": list(leader.colors),
                          "art": f"{art_base}/{index[leader.id]}" if leader.id in index else None,
                          "coverage": round(done / total, 3) if total else 1.0})
    def card_art(card_id: str) -> tuple[Optional[str], Optional[str]]:
        """(thumbnail, full size) for a card, if there's art for it."""
        full = index.get(card_id)
        if full is None:
            return None, None
        small = full.rpartition(".")[0] + "_small.jpg"
        return f"{art_base}/{small if small in have else full}", f"{art_base}/{full}"

    have = frozenset(files)
    pool: list[bytes] = []  # the deck builder's card list as JSON, made on the first request

    known = {b["sha256"] or b["spec"]: b for b in (analysis or {}).get("bots", [])}
    bot_list = []
    for name, spec in bots.items():
        a = known.get(_sha(spec) or spec, {})
        bot_list.append({"name": name, "kind": "network + search" if spec.startswith("mcts:") else
                         "network" if spec.endswith(".pt") else "rules",
                         "rating": a.get("rating"), "about": a.get("about"), "analysis_name": a.get("name")})

    oauth = None
    if cfg.google:
        from authlib.integrations.starlette_client import OAuth
        oauth = OAuth()
        oauth.register("google", client_id=cfg.google_id, client_secret=cfg.google_secret,
                       server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
                       client_kwargs={"scope": "openid email profile"})

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """Refuse big bodies and posts from other sites, and log each request."""
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return JSONResponse({"error": "request too large"}, 413)
        origin = request.headers.get("origin")
        if request.method in ("POST", "PATCH", "DELETE") and origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"error": "cross-site request"}, 403)
        t = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("request_failed", extra={"fields": {"path": request.url.path}})
            return JSONResponse({"error": "server error"}, 500)
        if not request.url.path.startswith(("/art/", "/healthz", "/assets/")):
            event("request", method=request.method, path=request.url.path, status=response.status_code,
                  ms=round((time.monotonic() - t) * 1000, 1))
        return response

    app.add_middleware(SessionMiddleware, secret_key=cfg.session_secret(), https_only=cfg.secure_cookies,
                       same_site="lax", max_age=30 * 24 * 3600)

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError):
        detail = "; ".join(f"{'.'.join(str(x) for x in e['loc'][1:]) or 'body'}: {e['msg']}" for e in exc.errors())
        return _error(detail)

    def user(request: Request):
        uid = request.session.get("uid")
        return st.store.get_user(uid) if uid else None

    def room(request: Request) -> Optional[Room]:
        return st.rooms.get(request.session.get("game"))

    def no_game():
        return _error("start a game from the Play page", 404, "no_game")

    def signed_out():
        return _error("sign in first", 401, "signed_out")

    def finished(r: Room) -> None:
        """Note a browser game that just ended, once."""
        s = r.session
        if s.g.over and not r.recorded:
            r.recorded = True
            reason = "concede" if s.conceded else "normal"
            st.store.add_game(r.user_id, s.bot_name, r.deck, r.bot_deck, s.human, s.g.winner, reason, s.g.turn_number)
            event("game_over", bot=s.bot_name, winner=s.g.winner, human=s.human, turns=s.g.turn_number, reason=reason)

    # ----- what the pages show -----

    @app.get("/api/me")
    def me(request: Request):
        u = user(request)
        r = room(request)
        return {"user": None if u is None else {"id": u["id"], "name": u["name"], "email": u["email"],
                                                 "since": u["created"]},
                "signin": "google" if cfg.google else "dev" if cfg.dev_login else None,
                "game": None if r is None else {"over": r.session.g.over, "bot": r.session.bot_name,
                                                "deck": r.deck, "bot_deck": r.bot_deck,
                                                "turns": r.session.g.turn_number}}

    @app.get("/api/options")
    def options():
        default = "rocks" if "rocks" in st.decks else next(iter(st.decks))
        return {"decks": deck_list, "bots": bot_list, "house_deck": st.house_deck, "default_deck": default}

    @app.get("/api/cards")
    def cards():
        if not pool:
            out = []
            for j in json.loads((DATA / "cards.json").read_text()):
                thumb, full = card_art(j["id"])
                out.append({k: j[k] for k in ("id", "name", "category", "colors", "cost", "power", "counter", "life",
                                              "types", "attributes", "rarity", "set")} |
                           {"text": j["effect"], "trigger": j["trigger"], "unlimited": UNLIMITED in j["effect"],
                            "art": thumb, "full": full})
            pool.append(json.dumps({"cards": out}, separators=(",", ":")).encode())
        return Response(pool[0], media_type="application/json", headers={"Cache-Control": "max-age=3600"})

    @app.get("/api/deck")
    def deck_cards(name: str = Query(max_length=200)):
        """A site deck's cards, for starting a deck in the builder from it."""
        d = st.decks.get(name)
        if d is None:
            return _error("no such deck", 404, "not_found")
        return {"name": name, "leader": d.leader.id, "cards": dict(Counter(c.id for c in d.cards))}

    @app.get("/decks")  # for bot authors: the names the bot API accepts
    def decks():
        return {"decks": list(st.decks), "house_deck": st.house_deck, "bots": list(bots)}

    @app.get("/api/analysis")
    def analysis_data():
        return {"analysis": analysis, "house": st.store.house_record(), "bots": bot_list}

    @app.get("/api/docs/bot-api", response_class=PlainTextResponse)
    def bot_docs():
        return DOCS.read_text() if DOCS.is_file() else "See docs/bot-api.md in the project."

    @app.get("/bot-api", include_in_schema=False)
    def bot_docs_page():
        return RedirectResponse("/docs/bot-api", 308)

    # ----- browser games -----

    @app.post("/api/games")
    def new_game(request: Request, body: NewGame):
        if (body.decklist is None and body.deck not in st.decks) or body.bot_deck not in st.decks or body.bot not in bots:
            return _error("pick a deck and a bot from the lists")
        if body.decklist is None:
            mine = st.decks[body.deck]
        else:
            if not body.deck.strip():
                return _error("name your deck")
            try:
                mine = parse_decklist(body.decklist)
                mine.validate()
            except DeckError as e:
                return _error(f"your deck can't be played: {e}", code="bad_deck")
        seat = secrets.randbelow(2) if body.seat is None else body.seat
        theirs = st.decks[body.bot_deck]
        s = Session((mine, theirs) if seat == 0 else (theirs, mine), bots[body.bot], seat, None, body.bot)
        u = user(request)
        token = st.rooms.add(Room(s, body.deck, body.bot_deck, u["id"] if u else None))
        if token is None:
            return _error("every table is in use; try again in a few minutes", 503, "busy")
        st.rooms.drop(request.session.get("game"))
        request.session["game"] = token
        event("game_start", bot=body.bot, decks=[body.deck, body.bot_deck], seat=seat, user=u["id"] if u else None,
              built=body.decklist is not None)
        return {"ok": True, "url": "/game"}

    @app.get("/game", response_class=HTMLResponse)
    def game(request: Request):
        r = room(request)
        if r is None:
            return RedirectResponse("/play", 303)
        with r.lock:
            return page(r.session.data(cfg.art_base, st.art_files)).replace(
                "<body>", '<body><a href="/" class="site-back" title="Back to the site">← Site</a>'
                          '<style>.site-back{position:fixed;left:12px;top:10px;z-index:50;font:600 13px/1 system-ui,'
                          'sans-serif;color:#e8e6df;background:rgba(20,20,20,.72);padding:7px 11px;border-radius:7px;'
                          'text-decoration:none;border:1px solid rgba(255,255,255,.14)}.site-back:hover{'
                          'background:rgba(40,40,40,.9)}</style>', 1)

    @app.get("/api/state")
    def state(request: Request, since: int = Query(0, ge=0, alias="from")):
        r = room(request)
        if r is None:
            return no_game()
        with r.lock:
            return r.session.state(since)

    @app.post("/api/act")
    def act(request: Request, body: Act):
        r = room(request)
        if r is None:
            return no_game()
        with r.lock:
            s = r.session
            try:
                s.act(body.frame, body.option)
            except ValueError as e:  # a stale click; the page catches up from the state sent back
                return JSONResponse({"error": str(e), **s.state(body.frame)}, 409)
            finished(r)
            return s.state(body.frame)

    @app.post("/api/concede")
    def concede(request: Request, body: dict = Body(default={})):
        r = room(request)
        if r is None:
            return no_game()
        with r.lock:
            r.session.concede()
            finished(r)
            return r.session.state(len(r.session.frames) - 1)

    @app.post("/api/new")
    def new(request: Request, body: dict = Body(default={})):
        """Another game with the same decks and bot (the table's New game button)."""
        r = room(request)
        if r is None:
            return no_game()
        with r.lock:
            s = r.session
            r.session = Session(s.decks, s.bot_spec, s.human, None, s.bot_name)
            r.recorded = False
        return {"ok": True}

    if local_art:
        @app.get("/art/{path:path}")
        def art_file(path: str):
            f = (ASSETS / path).resolve()
            if ASSETS.resolve() not in f.parents or not f.is_file():
                return PlainTextResponse("not found", 404)
            return Response(f.read_bytes(), media_type=mimetypes.guess_type(f.name)[0] or "application/octet-stream",
                            headers={"Cache-Control": "max-age=86400"})

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "games": len(st.rooms.rooms), "bot_matches": sum(matches.playing.values())}

    # ----- signing in (optional: guests can play) -----

    @app.get("/login")
    async def login(request: Request):
        if oauth is None:
            return RedirectResponse("/signin", 303)
        back = f"{cfg.public_url}/auth/google" if cfg.public_url else str(request.url_for("auth_google"))
        return await oauth.google.authorize_redirect(request, back)

    @app.get("/auth/google")
    async def auth_google(request: Request):
        if oauth is None:
            return RedirectResponse("/", 303)
        try:
            token = await oauth.google.authorize_access_token(request)
        except Exception as e:  # a refused consent, or a stale or forged callback
            event("login_failed", logging.WARNING, error=str(e))
            return RedirectResponse("/signin?failed=1", 303)
        info = token.get("userinfo") or {}
        if not info.get("sub"):
            return RedirectResponse("/signin?failed=1", 303)
        request.session["uid"] = st.store.user(f"google:{info['sub']}", info.get("email"), info.get("name"))
        event("login", user=request.session["uid"])
        return RedirectResponse("/account", 303)

    if cfg.dev_login:
        @app.get("/login/dev")
        def login_dev(request: Request, name: str = Query(min_length=1, max_length=32)):
            request.session["uid"] = st.store.user(f"dev:{name}", f"{name}@localhost", name)
            return RedirectResponse("/account", 303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.pop("uid", None)
        return RedirectResponse("/", 303)

    @app.post("/api/logout")
    def api_logout(request: Request):
        request.session.pop("uid", None)
        return {"ok": True}

    # ----- the account -----

    @app.get("/api/account")
    def account(request: Request):
        u = user(request)
        if u is None:
            return signed_out()
        return {"user": {"id": u["id"], "name": u["name"], "email": u["email"], "since": u["created"]},
                "totals": st.store.game_totals(u["id"]),
                "games": [{"bot": g["bot"], "deck": g["deck"], "bot_deck": g["bot_deck"], "turns": g["turns"],
                           "result": "draw" if g["winner"] is None else "win" if g["winner"] == g["seat"] else "loss",
                           "reason": g["reason"], "ended": g["ended"]} for g in st.store.games(u["id"])]}

    @app.patch("/api/account")
    def rename(request: Request, body: Named):
        u = user(request)
        if u is None:
            return signed_out()
        try:
            st.store.rename(u["id"], body.name)
        except ValueError as e:
            return _error(str(e))
        return {"ok": True}

    @app.delete("/api/account")
    def delete_account(request: Request):
        u = user(request)
        if u is None:
            return signed_out()
        for match_id in st.store.delete_user(u["id"]):
            (cfg.replays / f"{match_id}.json").unlink(missing_ok=True)
        request.session.clear()
        event("account_deleted", user=u["id"])
        return {"ok": True}

    # ----- bots and API keys -----

    def own_bot(request: Request, bot_id: int):
        u = user(request)
        b = st.store.bot(bot_id) if u else None
        return (u, b) if b is not None and b["user_id"] == u["id"] and b["retired"] is None else (u, None)

    @app.get("/api/bots")
    def list_bots(request: Request):
        u = user(request)
        if u is None:
            return signed_out()
        out = [{"id": b["id"], "name": b["name"], "created": b["created"], "record": st.store.record(b["id"]),
                "keys": [{"id": k["id"], "prefix": f"optcg_{k['prefix']}_", "created": k["created"]}
                         for k in st.store.keys(b["id"])]} for b in st.store.bots(u["id"])]
        games = [{"id": m["id"], "bot": m["bot"].split("#")[0], "opponent": m["opponent"], "started": m["started"],
                  "ended": m["ended"], "reason": m["reason"], "turns": m["turns"],
                  "result": None if m["ended"] is None else "draw" if m["winner"] is None
                  else "win" if m["winner"] == m["seat"] else "loss"} for m in st.store.matches(u["id"])]
        return {"bots": out, "matches": games, "limit": 10, "opponents": list(bots)}

    @app.post("/api/bots")
    def add_bot(request: Request, body: Named):
        u = user(request)
        if u is None:
            return signed_out()
        try:
            bot_id = st.store.add_bot(u["id"], body.name.strip())
        except ValueError as e:
            return _error(str(e))
        event("bot_created", user=u["id"], bot=body.name.strip())
        return {"ok": True, "id": bot_id}

    @app.delete("/api/bots/{bot_id}")
    def retire_bot(request: Request, bot_id: int):
        u, b = own_bot(request, bot_id)
        if b is None:
            return signed_out() if u is None else _error("no such bot", 404, "not_found")
        st.store.delete_bot(bot_id, u["id"])
        event("bot_retired", user=u["id"], bot=b["name"])
        return {"ok": True}

    @app.post("/api/bots/{bot_id}/keys")
    def add_key(request: Request, bot_id: int):
        u, b = own_bot(request, bot_id)
        if b is None:
            return signed_out() if u is None else _error("no such bot", 404, "not_found")
        key = st.store.new_key(bot_id)
        event("key_created", user=u["id"], bot=b["name"], prefix=key.split("_")[1])
        return {"key": key}

    @app.delete("/api/keys/{key_id}")
    def revoke(request: Request, key_id: int):
        u = user(request)
        if u is None:
            return signed_out()
        if not st.store.revoke(key_id, u["id"]):
            return _error("no such key", 404, "not_found")
        event("key_revoked", user=u["id"], key=key_id)
        return {"ok": True}

    # ----- the bot API and its replays -----

    @app.websocket("/api/bot/v1/play")
    async def bot_play(ws: WebSocket):
        await matches.run(ws)

    @app.get("/matches/{match_id}", response_class=HTMLResponse)
    def replay(match_id: str):
        f = cfg.replays / f"{match_id}.json"
        if not match_id.isalnum() or not f.is_file():
            return PlainTextResponse("no such match", 404)
        rep = json.loads(f.read_text())
        if a := art(rep, files=st.art_files):
            rep["art"] = {**a, "base": art_base}
        return page(rep)

    @app.get("/api/{rest:path}", include_in_schema=False)
    def no_api(rest: str):
        return _error(f"no such endpoint /api/{rest}", 404, "not_found")

    # ----- the React app -----

    @app.get("/assets/{path:path}", include_in_schema=False)
    def site_asset(path: str):
        f = (SITE / "assets" / path).resolve()
        if (SITE / "assets").resolve() not in f.parents or not f.is_file():
            return PlainTextResponse("not found", 404)
        return FileResponse(f, headers={"Cache-Control": "public, max-age=31536000, immutable"})  # names carry a hash

    @app.get("/{path:path}", include_in_schema=False)
    def site(path: str):
        path = path.strip("/")
        if (SITE / path).is_file() and path in ("favicon.svg", "robots.txt"):
            return FileResponse(SITE / path)
        if path not in PAGES:
            return HTMLResponse(_shell("Not found"), 404) if not (SITE / "index.html").is_file() else \
                HTMLResponse((SITE / "index.html").read_text(), 404)
        if not (SITE / "index.html").is_file():
            return HTMLResponse(_shell("The site isn't built: run npm run build in web/"), 503)
        return HTMLResponse((SITE / "index.html").read_text(), headers={"Cache-Control": "no-cache"})

    return app


def _shell(message: str) -> str:
    return (f'<!doctype html><meta charset="utf-8"><title>OPTCG bot</title>'
            f'<p style="font:16px system-ui;padding:40px">{message}</p>')
