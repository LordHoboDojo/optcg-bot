"""The hosted site: many people playing house bots in the browser, optional Google sign-in, and
the bot API for programs that play over a WebSocket.

    create_app(Config.from_env())  # see __main__.py to run it
"""
from __future__ import annotations

import html
import json
import logging
import mimetypes
import secrets
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from fastapi import Body, FastAPI, Form, Query, Request, WebSocket
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from pydantic import BaseModel, Field, StrictInt
from starlette.middleware.sessions import SessionMiddleware

from ..deck import Deck
from ..play import make_agent
from ..replay import ASSETS, art, art_files, page
from ..web import MAX_BODY, Session
from .botapi import BotMatches
from .config import Config
from .logs import event, log
from .store import Store

DOCS = Path(__file__).resolve().parents[2] / "docs" / "bot-api.md"


@dataclass
class Room:
    """One visitor's game in the browser."""
    session: Session
    lock: threading.Lock = field(default_factory=threading.Lock)
    last: float = field(default_factory=time.monotonic)


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

    def add(self, session: Session) -> Optional[str]:
        """A token for the new game, or None when the server is full."""
        with self.lock:
            now = time.monotonic()
            for t in [t for t, r in self.rooms.items() if now - r.last > self.idle]:
                del self.rooms[t]
            if len(self.rooms) >= self.cap:
                return None
            token = secrets.token_urlsafe(16)
            self.rooms[token] = Room(session)
            return token

    def drop(self, token: Optional[str]) -> None:
        with self.lock:
            self.rooms.pop(token or "", None)


class Act(BaseModel):
    frame: StrictInt = Field(ge=0)
    option: StrictInt = Field(ge=0)


def create_app(cfg: Config) -> FastAPI:
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    st = app.state
    st.cfg, st.store = cfg, Store(cfg.db)
    st.decks: dict[str, Deck] = cfg.load_decks()
    st.house_deck = cfg.house_deck if cfg.house_deck in st.decks else next(iter(st.decks))
    st.rooms = Rooms(cfg.max_games, cfg.idle_minutes)
    st.art_files = cfg.art_files()
    local_art = cfg.art_base == "art" and bool(art_files())
    for spec in cfg.bots:
        make_agent(spec)  # fail at startup, not on the first game, if a house bot can't load
    matches = BotMatches(app)

    oauth = None
    if cfg.google:
        from authlib.integrations.starlette_client import OAuth
        oauth = OAuth()
        oauth.register("google", client_id=cfg.google_id, client_secret=cfg.google_secret,
                       server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
                       client_kwargs={"scope": "openid email profile"})

    @app.middleware("http")
    async def guard(request: Request, call_next):
        """Refuse big bodies and form posts from other sites, and log each request."""
        if int(request.headers.get("content-length") or 0) > MAX_BODY:
            return JSONResponse({"error": "request too large"}, 413)
        origin = request.headers.get("origin")
        if request.method == "POST" and origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"error": "cross-site request"}, 403)
        t = time.monotonic()
        try:
            response = await call_next(request)
        except Exception:
            log.exception("request_failed", extra={"fields": {"path": request.url.path}})
            return JSONResponse({"error": "server error"}, 500)
        if not request.url.path.startswith(("/art/", "/healthz")):
            event("request", method=request.method, path=request.url.path, status=response.status_code,
                  ms=round((time.monotonic() - t) * 1000, 1))
        return response

    app.add_middleware(SessionMiddleware, secret_key=cfg.session_secret(), https_only=cfg.secure_cookies,
                       same_site="lax", max_age=30 * 24 * 3600)

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError):
        detail = "; ".join(f"{'.'.join(str(x) for x in e['loc'][1:]) or 'body'}: {e['msg']}" for e in exc.errors())
        return JSONResponse({"error": "bad_request", "detail": detail}, 400)

    def user(request: Request):
        uid = request.session.get("uid")
        return st.store.get_user(uid) if uid else None

    def room(request: Request) -> Optional[Room]:
        return st.rooms.get(request.session.get("game"))

    def no_game():
        return JSONResponse({"error": "no_game", "detail": "start a game from the lobby"}, 404)

    # ----- the lobby and browser games -----

    @app.get("/", response_class=HTMLResponse)
    def lobby(request: Request):
        return _page("Play", _lobby(st, user(request), cfg), user(request), cfg)

    @app.get("/decks")
    def decks():
        return {"decks": list(st.decks), "house_deck": st.house_deck, "bots": cfg.bots}

    @app.post("/play")
    def play(request: Request, deck: str = Form(), bot_deck: str = Form(), bot: str = Form()):
        if deck not in st.decks or bot_deck not in st.decks or bot not in cfg.bots:
            return JSONResponse({"error": "bad_request", "detail": "pick a deck and a bot from the lists"}, 400)
        token = st.rooms.add(Session((st.decks[deck], st.decks[bot_deck]), bot, 0))
        if token is None:
            return HTMLResponse(_page("Busy", "<p>Every table is in use. Try again in a few minutes.</p>",
                                      user(request), cfg), 503)
        st.rooms.drop(request.session.get("game"))
        request.session["game"] = token
        event("game_start", bot=bot, decks=[deck, bot_deck])
        return RedirectResponse("/game", 303)

    @app.get("/game", response_class=HTMLResponse)
    def game(request: Request):
        r = room(request)
        if r is None:
            return RedirectResponse("/", 303)
        with r.lock:
            return page(r.session.data(cfg.art_base, st.art_files))

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
            if s.g.over:
                event("game_over", bot=s.bot_spec, winner=s.g.winner, human=s.human, turns=s.g.turn_number)
            return s.state(body.frame)

    @app.post("/api/concede")
    def concede(request: Request, body: dict = Body(default={})):
        r = room(request)
        if r is None:
            return no_game()
        with r.lock:
            r.session.concede()
            return r.session.state(len(r.session.frames) - 1)

    @app.post("/api/new")
    def new(request: Request, body: dict = Body(default={})):
        r = room(request)
        if r is None:
            return no_game()
        with r.lock:
            s = r.session
            r.session = Session(s.decks, s.bot_spec, s.human)
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
            return PlainTextResponse("Google sign-in isn't set up on this server", 404)
        back = f"{cfg.public_url}/auth/google" if cfg.public_url else str(request.url_for("auth_google"))
        return await oauth.google.authorize_redirect(request, back)

    @app.get("/auth/google")
    async def auth_google(request: Request):
        if oauth is None:
            return PlainTextResponse("Google sign-in isn't set up on this server", 404)
        try:
            token = await oauth.google.authorize_access_token(request)
        except Exception as e:  # a refused consent, or a stale or forged callback
            event("login_failed", logging.WARNING, error=str(e))
            return RedirectResponse("/", 303)
        info = token.get("userinfo") or {}
        if not info.get("sub"):
            return RedirectResponse("/", 303)
        request.session["uid"] = st.store.user(f"google:{info['sub']}", info.get("email"), info.get("name"))
        event("login", user=request.session["uid"])
        return RedirectResponse("/bots", 303)

    if cfg.dev_login:
        @app.get("/login/dev")
        def login_dev(request: Request, name: str = Query(min_length=1, max_length=32)):
            request.session["uid"] = st.store.user(f"dev:{name}", f"{name}@localhost", name)
            return RedirectResponse("/bots", 303)

    @app.post("/logout")
    def logout(request: Request):
        request.session.pop("uid", None)
        return RedirectResponse("/", 303)

    # ----- bots and API keys (signed in) -----

    @app.get("/bots", response_class=HTMLResponse)
    def bots(request: Request):
        u = user(request)
        if u is None:
            return RedirectResponse("/", 303)
        return _page("Your bots", _bots(st, u), u, cfg)

    @app.post("/bots", response_class=HTMLResponse)
    def add_bot(request: Request, name: str = Form()):
        u = user(request)
        if u is None:
            return RedirectResponse("/", 303)
        try:
            st.store.add_bot(u["id"], name.strip())
        except ValueError as e:
            return HTMLResponse(_page("Your bots", _bots(st, u, error=str(e)), u, cfg), 400)
        event("bot_created", user=u["id"], bot=name.strip())
        return RedirectResponse("/bots", 303)

    @app.post("/bots/{bot_id}/keys", response_class=HTMLResponse)
    def add_key(request: Request, bot_id: int):
        u = user(request)
        b = st.store.bot(bot_id)
        if u is None or b is None or b["user_id"] != u["id"]:
            return RedirectResponse("/bots", 303)
        key = st.store.new_key(bot_id)
        event("key_created", user=u["id"], bot=b["name"], prefix=key.split("_")[1])
        return _page("Your bots", _bots(st, u, new_key=(b["name"], key)), u, cfg)

    @app.post("/keys/{key_id}/revoke")
    def revoke(request: Request, key_id: int):
        u = user(request)
        if u is not None and st.store.revoke(key_id, u["id"]):
            event("key_revoked", user=u["id"], key=key_id)
        return RedirectResponse("/bots", 303)

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
            rep["art"] = {**a, "base": cfg.art_base if cfg.art_base.startswith("http") else "/art"}
        return page(rep)

    @app.get("/bot-api", response_class=PlainTextResponse)
    def bot_docs():
        return DOCS.read_text() if DOCS.is_file() else "See docs/bot-api.md in the project."

    return app


# ----- pages -----

CSS = """
:root { --bg: #f6f4ef; --fg: #1f1e1b; --muted: #6b6a64; --line: #d9d6cc; --card: #fff; --accent: #2f5fa7; }
@media (prefers-color-scheme: dark) { :root { --bg: #1b1b1a; --fg: #ecebe6; --muted: #a3a29b; --line: #3a3936;
  --card: #252523; --accent: #7aa7e8; } }
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--fg); font: 16px/1.6 system-ui, sans-serif; }
header { display: flex; justify-content: space-between; align-items: center; padding: 12px 16px;
  border-bottom: 1px solid var(--line); gap: 12px; flex-wrap: wrap; }
header a, header button { color: var(--accent); background: none; border: 0; font: inherit; cursor: pointer; padding: 0; }
main { max-width: 760px; margin: 0 auto; padding: 24px 16px; }
h1 { font-size: 22px; font-weight: 600; margin: 0 0 16px; } h2 { font-size: 18px; margin: 28px 0 8px; }
form.card, section.card { background: var(--card); border: 1px solid var(--line); border-radius: 10px; padding: 16px; }
label { display: block; margin: 0 0 12px; color: var(--muted); font-size: 14px; }
select, input[type=text] { display: block; width: 100%; margin-top: 4px; padding: 8px; font: inherit; color: var(--fg);
  background: var(--bg); border: 1px solid var(--line); border-radius: 6px; }
button.primary { background: var(--accent); color: #fff; border: 0; border-radius: 6px; padding: 9px 18px; font: inherit; cursor: pointer; }
table { width: 100%; border-collapse: collapse; font-size: 14px; } td, th { text-align: left; padding: 6px 4px;
  border-bottom: 1px solid var(--line); } th { color: var(--muted); font-weight: 500; }
code, .key { font-family: ui-monospace, monospace; font-size: 13px; overflow-wrap: anywhere; }
.key { display: block; padding: 10px; background: var(--bg); border: 1px dashed var(--accent); border-radius: 6px; margin: 8px 0; }
.error { color: #c0392b; } .muted { color: var(--muted); font-size: 14px; }
form.inline { display: inline; } button.link { color: var(--accent); background: none; border: 0; padding: 0; font: inherit; cursor: pointer; } .row { display: flex; gap: 8px; align-items: end; } .row label { flex: 1; margin: 0; }
"""


def _page(title: str, body: str, u, cfg: Config) -> str:
    e = html.escape
    if u is not None:
        who = (f'<span>{e(u["name"] or u["email"] or "you")}</span> <a href="/bots">Your bots</a> '
               '<form class="inline" method="post" action="/logout"><button>Sign out</button></form>')
    elif cfg.google:
        who = '<a href="/login">Sign in with Google</a>'
    elif cfg.dev_login:
        who = '<a href="/login/dev?name=dev">Sign in (dev)</a>'
    else:
        who = ""
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" '
            f'content="width=device-width, initial-scale=1"><title>{e(title)} · OPTCG bot</title>'
            f'<style>{CSS}</style></head><body><header><a href="/"><b>OPTCG bot</b></a><nav>{who}</nav></header>'
            f'<main>{body}</main></body></html>')


def _options(names, chosen) -> str:
    e = html.escape
    return "".join(f'<option value="{e(n)}"{" selected" if n == chosen else ""}>{e(n)}</option>' for n in names)


def _lobby(st, u, cfg: Config) -> str:
    mine = "rocks" if "rocks" in st.decks else next(iter(st.decks))
    signin = "" if u is not None else (
        '<p class="muted">Sign in to register a bot of your own and play it against these over the '
        '<a href="/bot-api">bot API</a>.</p>' if (cfg.google or cfg.dev_login) else "")
    return (f'<h1>Play a house bot</h1><form class="card" method="post" action="/play">'
            f'<label>Your deck<select name="deck">{_options(st.decks, mine)}</select></label>'
            f'<label>Bot\'s deck<select name="bot_deck">{_options(st.decks, st.house_deck)}</select></label>'
            f'<label>Bot<select name="bot">{_options(cfg.bots, cfg.bots[0])}</select></label>'
            f'<button class="primary">Play</button></form>'
            f'<p class="muted">No account needed. A game left alone for {cfg.idle_minutes:g} minutes ends.</p>{signin}')


def _bots(st, u, error: str = "", new_key: Optional[tuple[str, str]] = None) -> str:
    e = html.escape
    out = ["<h1>Your bots</h1>"]
    if new_key:
        out.append(f'<section class="card"><b>New API key for {e(new_key[0])}</b>'
                   f'<span class="key">{e(new_key[1])}</span><span class="muted">Copy it now: it won\'t be shown '
                   'again. Send it as <code>Authorization: Bearer &lt;key&gt;</code> when your bot connects to '
                   '<code>/api/bot/v1/play</code>.</span></section>')
    for b in st.store.bots(u["id"]):
        keys = st.store.keys(b["id"])
        rows = "".join(f'<tr><td><code>optcg_{e(k["prefix"])}_…</code></td>'
                       f'<td>{time.strftime("%Y-%m-%d", time.localtime(k["created"]))}</td>'
                       f'<td><form class="inline" method="post" action="/keys/{k["id"]}/revoke">'
                       f'<button class="link">Revoke</button></form></td></tr>' for k in keys)
        out.append(f'<h2>{e(b["name"])}</h2><section class="card">'
                   + (f'<table><tr><th>Key</th><th>Created</th><th></th></tr>{rows}</table>' if keys
                      else '<p class="muted">No keys yet.</p>')
                   + f'<form method="post" action="/bots/{b["id"]}/keys"><button class="primary">New API key'
                     f'</button></form></section>')
    out.append('<h2>Register a bot</h2><form class="card" method="post" action="/bots"><div class="row">'
               '<label>Name<input type="text" name="name" required minlength="3" maxlength="32" '
               'pattern="[A-Za-z0-9_\\-]+" placeholder="my-bot"></label><button class="primary">Add</button></div>'
               + (f'<p class="error">{e(error)}</p>' if error else "") + "</form>")
    games = st.store.matches(u["id"])
    if games:
        rows = "".join(
            f'<tr><td>{time.strftime("%Y-%m-%d %H:%M", time.localtime(m["started"]))}</td><td>{e(m["bot"])}</td>'
            f'<td>{e(m["opponent"])}</td><td>{_result(m)}</td><td>{e(m["reason"] or "playing")}</td>'
            f'<td>{"<a href=/matches/" + m["id"] + ">Replay</a>" if m["ended"] else ""}</td></tr>' for m in games)
        out.append(f'<h2>Recent matches</h2><section class="card"><table><tr><th>Started</th><th>Bot</th>'
                   f'<th>Opponent</th><th>Result</th><th>How</th><th></th></tr>{rows}</table></section>')
    out.append('<p class="muted">How to connect: <a href="/bot-api">the bot API</a>. '
               'There\'s an example client in <code>tools/bot_client.py</code>.</p>')
    return "".join(out)


def _result(m) -> str:
    if m["ended"] is None:
        return ""
    return "Draw" if m["winner"] is None else "Win" if m["winner"] == m["seat"] else "Loss"
