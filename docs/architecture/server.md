# Web server (`optcg/server/`)

A FastAPI app that serves the React site and its JSON API, the game table, Google sign-in,
accounts, bots and API keys, and the WebSocket bot API. It runs under uvicorn
(`python -m optcg.server`), behind Caddy in production (see [Deployment](deployment.md)).

| File | Role |
|---|---|
| `__main__.py` | Command line: `--host`, `--port` (or `HOST`, `PORT`), `--art-manifest FILE` (write the art file list and exit). Sets up JSON logs and runs uvicorn with proxy headers trusted |
| `config.py` | `Config.from_env()`: every setting from environment variables; the house bots and the deck list |
| `app.py` | `create_app(cfg)`: middleware, the JSON API, browser games (`Rooms`), sign-in, account, bots and keys, replays, the React app's routes |
| `botapi.py` | `BotMatches`: the WebSocket bot API, one match per connection |
| `store.py` | `Store`: SQLite (see [Database](database.md)) |
| `logs.py` | One JSON object per log line on stdout (`event(name, **fields)`), for CloudWatch |

It builds on `optcg/web.py` (`Session`, one game with one hidden seat, see
[Python package](python.md#websession-one-game-one-hidden-seat)) and `optcg/replay.py` (the table
page and card art).

## Components and state

```mermaid
flowchart TB
    client(("Browser")) --> mw
    bot(("Remote bot")) -- WebSocket --> ws
    subgraph app["FastAPI app (app.py)"]
        mw["guard middleware<br/>size limit, cross-site check, request log"]
        sess["SessionMiddleware<br/>signed cookie: uid, game"]
        api["/api/* JSON routes"]
        pages["/game, /matches/id, /art, /assets,<br/>React app routes"]
        oauth["/login, /auth/google, /login/dev, /logout"]
        ws["/api/bot/v1/play"]
        mw --> sess --> api
        sess --> pages
        sess --> oauth
    end
    rooms["Rooms (memory)<br/>token to Room: Session, decks, user, lock<br/>cap OPTCG_MAX_GAMES, idle OPTCG_IDLE_MINUTES"]
    matches["BotMatches (memory)<br/>playing: bot id to matches in progress"]
    store[("Store: SQLite<br/>users, bots, keys, matches, games")]
    replays[/"replays folder<br/>match id .json"/]
    decks["app.state.decks<br/>Config.load_decks()"]
    house["house bots<br/>Config.house_bots, make_agent()"]
    api --> rooms
    api --> store
    ws --> matches
    matches --> store
    matches --> replays
    pages --> rooms
    pages --> replays
    oauth --> store
    rooms --> house
    matches --> house
    api --> decks
    ws --> decks
```

- **Live browser games** are `Room`s in memory, keyed by a random token stored in the visitor's
  session cookie (`game`). A restart or deploy ends them all. A room left idle for
  `OPTCG_IDLE_MINUTES` is dropped the next time a game is created, and at most
  `OPTCG_MAX_GAMES` exist at once. Run one copy of the app.
- **Bot API matches** live for the length of their WebSocket. `BotMatches.playing` counts each
  bot's matches in progress.
- **Durable data** (accounts, bots, keys, match and game records) is in SQLite at `OPTCG_DB`;
  bot match replays are JSON files in `OPTCG_REPLAYS`.
- **The session cookie** is Starlette's signed cookie (`OPTCG_SECRET`; a random secret, so
  sessions end on restart, if unset), `SameSite=Lax`, 30 days, HTTPS-only when
  `OPTCG_SECURE_COOKIES=1`. It holds `uid` (signed in) and `game` (the room token).

At startup `create_app` loads the decks, builds every house bot once with `make_agent` (so a
bad checkpoint fails at startup, not on the first game), reads `data/analysis.json` if present,
and registers Google OAuth if `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` are set.

## Every request: the guard middleware

```mermaid
flowchart TB
    r["request"] --> big{"Content-Length over<br/>MAX_BODY (64 KB)?"}
    big -- yes --> e413["413 request too large"]
    big -- no --> xs{"POST, PATCH or DELETE<br/>with an Origin from another host?"}
    xs -- yes --> e403["403 cross-site request"]
    xs -- no --> run["route handler"]
    run -- "exception" --> e500["500 server error<br/>log request_failed with the traceback"]
    run -- "response" --> log["log request: method, path, status, ms<br/>(not /art, /assets, /healthz)"]
    run -- "body fails validation" --> e400["400 bad_request<br/>with the fields that failed"]
```

JSON errors from the API are `{"error": code, "detail": message}`. Codes: `bad_request` (400),
`bad_deck` (400), `signed_out` (401), `not_found` (404), `no_game` (404), `busy` (503), and a
`409` with the current state for a stale move.

## Routes

| Method and path | Who | What it does | Errors |
|---|---|---|---|
| `GET /api/me` | anyone | The signed-in user, the sign-in method (`google`, `dev` or none) and the current game | |
| `GET /api/options` | anyone | Decks (with Leader, colors, art, coverage), house bots (with rating from the analysis), house and default deck | |
| `GET /api/cards` | anyone | The whole card pool for the deck builder, built on first request and cached (`max-age=3600`) | |
| `GET /api/deck?name=` | anyone | A site deck's Leader and card counts | 404 `not_found` |
| `GET /decks` | anyone | Deck names, house deck and house bot names, for bot authors | |
| `GET /api/analysis` | anyone | `analysis.json`, the house bots' records from the database, and the bot list | |
| `GET /api/docs/bot-api` | anyone | `docs/bot-api.md` as text | |
| `POST /api/games` | anyone | Start a browser game | 400 `bad_request`, 400 `bad_deck`, 503 `busy` |
| `GET /game` | room owner | The table page with the game's data | 303 to `/play` without a game |
| `GET /api/state?from=N` | room owner | Frames from N on, `over`, `winner`, `turns`, `first` | 404 `no_game` |
| `POST /api/act` | room owner | Answer the decision in frame `frame` with `option` | 404 `no_game`, 409 stale, 400 |
| `POST /api/concede` | room owner | Concede; returns the final state | 404 `no_game` |
| `POST /api/new` | room owner | Same decks and bot, new game | 404 `no_game` |
| `GET /login`, `GET /auth/google` | anyone | Google sign-in | redirects to `/signin?failed=1` |
| `GET /login/dev?name=` | anyone, only with `OPTCG_DEV_LOGIN=1` | Sign in without Google | |
| `POST /logout`, `POST /api/logout` | anyone | Forget `uid` | |
| `GET`, `PATCH`, `DELETE /api/account` | signed in | Account with game history; rename; delete everything | 401 `signed_out`, 400 |
| `GET`, `POST /api/bots` | signed in | List bots, keys and matches; register a bot | 401, 400 |
| `DELETE /api/bots/{id}` | owner | Retire a bot (its keys stop working) | 401, 404 |
| `POST /api/bots/{id}/keys` | owner | A new API key, shown once | 401, 404 |
| `DELETE /api/keys/{id}` | owner | Revoke a key | 401, 404 |
| `WS /api/bot/v1/play` | API key | Play a match (below) | close codes 4400, 4401, 4429 |
| `GET /matches/{id}` | anyone with the id | A bot match's replay on the table | 404 |
| `GET /art/{path}` | anyone | Card art from `StreamingAssets/`, only when art is local | 404 |
| `GET /assets/{path}` | anyone | The built site's hashed files, cached for a year | 404 |
| `GET /healthz` | anyone | `{"ok", "games", "bot_matches"}` | |
| `GET /api/{anything else}` | anyone | | 404 `not_found` |
| `GET /{page}` | anyone | `web/dist/index.html` for the React routes; 404 page otherwise; 503 if the site isn't built | |

## Browser game

```mermaid
sequenceDiagram
    actor U as Person
    participant P as Play page (React)
    participant S as Server (app.py)
    participant R as Rooms
    participant Ses as web.Session
    participant G as optcg_rs.Game
    participant DB as Store
    U->>P: pick decks and a house bot, Start
    P->>S: POST /api/games {deck, bot_deck, bot, decklist?}
    alt a deck or bot not in the lists
        S-->>P: 400 bad_request "pick a deck and a bot from the lists"
    else a built decklist that fails parse or validate
        S-->>P: 400 bad_deck "your deck can't be played: ..."
    else
        S->>Ses: Session(decks, bot spec, random seat)
        Ses->>G: new game, bot plays until the person decides
        S->>R: add(Room), drop idle rooms
        alt OPTCG_MAX_GAMES rooms already
            R-->>S: None
            S-->>P: 503 busy "every table is in use"
        else
            R-->>S: token
            S->>S: drop the old room, cookie game = token
            S-->>P: 200 {url: "/game"}
            P->>S: GET /game
            S-->>U: table page (replay_viewer.html, live mode)
            U->>S: GET /api/state?from=0
            S-->>U: frames so far
        end
    end
    loop each decision
        U->>S: POST /api/act {frame, option}
        alt no room for the cookie (expired, idle, restart)
            S-->>U: 404 no_game
        else frame is not the latest, or a bad option
            S-->>U: 409 {error, ...current state}, the page catches up
        else
            S->>Ses: act(frame, option) under the room lock
            Ses->>G: step(option), then the bot plays to the person's next decision
            opt the game just ended
                S->>DB: add_game(user, bot, decks, seat, winner, "normal", turns)
            end
            S-->>U: new frames, over, winner
        end
    end
    opt the person concedes
        U->>S: POST /api/concede
        S->>Ses: concede()
        S->>DB: add_game(..., reason "concede")
        S-->>U: final state
    end
    opt New game on the table
        U->>S: POST /api/new
        S->>R: same room, new Session (same decks, bot, seat)
        S-->>U: {ok}, the page reloads
    end
```

Notes:

- Guests can play; `games.user_id` is then empty. A finished game is recorded once per room
  (`Room.recorded`).
- A request for a room that has gone (restart, deploy, idle) gets `404 no_game`, and `GET /game`
  redirects to `/play`.
- The table polls nothing: every answer to `/api/act` carries the frames the bot played since,
  and the page animates them.

## Bot API match

The protocol, message by message, is in [`docs/bot-api.md`](../bot-api.md). The server side is
`botapi.py`. Close codes: `1000` normal, `4400` bad request, `4401` unauthorized, `4429` too many
matches.

### Happy path

```mermaid
sequenceDiagram
    participant B as Remote bot
    participant M as BotMatches (botapi.py)
    participant DB as Store
    participant Ses as web.Session
    participant F as Replay files
    B->>M: WebSocket /api/bot/v1/play, Authorization Bearer key
    M->>M: accept()
    M->>DB: bot_for_key(key): prefix lookup, constant-time hash compare, not revoked
    DB-->>M: bot row
    M->>M: playing[bot] < OPTCG_BOT_MATCHES, then playing[bot] += 1
    B->>M: {"type": "start", "deck", "opponent", "opponent_deck", "seat"} within 30 s
    M->>M: _read_start: seat, house bot, decks by name or decklist
    M->>Ses: Session(decks, house bot, seat) in a thread
    M->>DB: start_match(id, bot, opponent, seat, leader names, seed)
    M-->>B: {"type": "match", match, seat, opponent, players, cards, timeout}
    loop until the game is over
        M-->>B: {"type": "decision", decision, kind, prompt, options, actions, frames}
        B->>M: {"type": "move", decision, option} within OPTCG_MOVE_TIMEOUT
        M->>Ses: act(decision, option) in a thread: the house bot plays on
    end
    M->>DB: end_match(id, winner, "normal", turns)
    M->>F: write replays/id.json
    M-->>B: {"type": "over", winner, result, reason "normal", turns, frames, replay "/matches/id"}
    M->>B: close 1000
    M->>M: playing[bot] -= 1
```

### Refused before a match starts

No match is recorded for any of these.

```mermaid
sequenceDiagram
    participant B as Remote bot
    participant M as BotMatches
    participant DB as Store
    B->>M: WebSocket connect
    M->>M: accept()
    alt no "Authorization: Bearer" header, bad key format, unknown or revoked key
        M->>DB: bot_for_key(key)
        DB-->>M: None
        M-->>B: {"type": "error", "error": "unauthorized"}
        M->>B: close 4401
    else the bot already plays OPTCG_BOT_MATCHES matches
        M-->>B: {"type": "error", "error": "too_many_matches"}
        M->>B: close 4429
    else no start message within 30 s, or the bot disconnects first
        M-->>B: {"type": "error", "error": "bad_request", "detail": "send a start message after connecting"}
        M->>B: close 4400
    else the start message is wrong
        Note over M: not JSON text or over 64 KB, type not "start", seat not 0 or 1,<br/>unknown opponent, unknown deck name, bad decklist
        M-->>B: {"type": "error", "error": "bad_request", "detail": reason}
        M->>B: close 4400
    end
```

### Ending a match early: forfeits

Once a match has started, an illegal move, running out of time, conceding or disconnecting ends
it at once as a **loss for the remote bot** (`Forfeit`, then `Session.concede()`). The match is
still recorded and its replay saved, with the forfeit as `reason`.

```mermaid
sequenceDiagram
    participant B as Remote bot
    participant M as BotMatches._move
    participant Ses as web.Session
    participant DB as Store
    M-->>B: {"type": "decision", "decision": n, ...}
    alt the option is not an integer from 0 to options - 1
        B->>M: {"type": "move", "decision": n, "option": 99}
        M->>M: log illegal_move (warning)
        M-->>B: {"type": "error", "error": "illegal_move", "decision": n}
        M->>Ses: concede(), reason "illegal_move"
    else no move before the deadline
        M-->>B: {"type": "error", "error": "timeout"}
        M->>Ses: concede(), reason "timeout"
    else the bot concedes
        B->>M: {"type": "concede"}
        M->>Ses: concede(), reason "concede"
    else the socket closes
        B--xM: disconnect
        M->>Ses: concede(), reason "disconnect"
    end
    M->>DB: end_match(id, winner = house bot, reason, turns)
    M->>M: save the replay
    M-->>B: {"type": "over", "result": "loss", "reason": reason, ...} (skipped if disconnected)
    M->>B: close 1000 (if still connected)
```

### Messages that don't end the match

These get an `error` message and the server keeps waiting. **The clock keeps running**: the
deadline is set once per decision.

```mermaid
sequenceDiagram
    participant B as Remote bot
    participant M as BotMatches._move
    M-->>B: decision n (deadline = now + OPTCG_MOVE_TIMEOUT)
    B->>M: not JSON, binary, or over 64 KB
    M-->>B: error bad_request
    B->>M: {"type": "hello"} or a move without an integer decision
    M-->>B: error bad_request "send {type: move, decision, option}"
    B->>M: {"type": "move", "decision": n - 1, ...}
    M-->>B: error stale_decision "the decision waiting is n"
    B->>M: {"type": "move", "decision": n, "option": 2}
    M->>M: legal: play it
```

### Match states

```mermaid
stateDiagram-v2
    [*] --> Connected: accept()
    Connected --> Refused: bad key (4401) or too many matches (4429)
    Connected --> WaitingStart: authorized
    WaitingStart --> Refused: no start in 30 s, disconnect, bad start (4400)
    WaitingStart --> Playing: valid start, match recorded, "match" sent
    Playing --> Playing: legal move
    Playing --> Playing: malformed or stale message (error, clock runs)
    Playing --> Over: game over (reason normal)
    Playing --> Over: illegal_move, timeout, concede, disconnect (loss)
    Over --> [*]: end_match, replay saved, "over" sent, close 1000
    Refused --> [*]
```

The server logs `match_start`, `illegal_move` and `match_over` (with `result`, `reason`,
`turns`, `seconds`) as JSON events.

## Signing in

Sign-in is optional: guests can play. Google is used when `GOOGLE_CLIENT_ID` and
`GOOGLE_CLIENT_SECRET` are set (Authlib, OpenID Connect, scopes `openid email profile`). The
account is keyed by Google's `sub` (stored as `google:<sub>`), which stays the same when the
person's email changes.

```mermaid
sequenceDiagram
    actor U as Person
    participant S as Server
    participant Go as Google
    participant DB as Store
    U->>S: GET /login
    alt Google not configured
        S-->>U: 303 /signin
    else
        S-->>U: 302 to Google, with state in the session cookie<br/>redirect URI = OPTCG_PUBLIC_URL/auth/google
        U->>Go: consent
        Go-->>U: 302 /auth/google?code&state
        U->>S: GET /auth/google
        S->>Go: exchange the code, read userinfo
        alt consent refused, stale or forged callback (state mismatch)
            S->>S: log login_failed (warning)
            S-->>U: 303 /signin?failed=1
        else no sub in userinfo
            S-->>U: 303 /signin?failed=1
        else
            S->>DB: user("google:" + sub, email, name): insert or update email and name
            DB-->>S: user id
            S->>S: cookie uid = id, log login
            S-->>U: 303 /account
        end
    end
    opt OPTCG_DEV_LOGIN=1 (local and tests only)
        U->>S: GET /login/dev?name=alice
        S->>DB: user("dev:alice", "alice@localhost", "alice")
        S-->>U: 303 /account
    end
    U->>S: POST /api/logout
    S->>S: remove uid from the cookie
```

Routes that need an account answer `401 signed_out` without `uid`; the React pages then show a
sign-in prompt.

With only `OPTCG_DEV_LOGIN=1` (no Google), the site's "Sign in (dev)" buttons go to `/login`,
which redirects to `/signin`; that page then asks for a name and submits it to
`/login/dev?name=<name>`.

## Bots and API keys

```mermaid
sequenceDiagram
    actor U as Person
    participant P as Your bots page
    participant S as Server
    participant DB as Store
    P->>S: GET /api/bots
    alt signed out
        S-->>P: 401 signed_out
    else
        S-->>P: bots (record, active keys by prefix), last 20 matches, limit, opponents
    end
    U->>P: register "my-bot"
    P->>S: POST /api/bots {name}
    alt name not 3 to 32 of A-Z a-z 0-9 - _
        S-->>P: 400 "a bot's name is 3 to 32 letters, digits, - or _"
    else already 10 bots
        S-->>P: 400 "an account can have at most 10 bots"
    else name taken (unique across all accounts)
        S-->>P: 400 "the name ... is taken"
    else
        S->>DB: insert bot
        S-->>P: {ok, id}
    end
    U->>P: create key
    P->>S: POST /api/bots/{id}/keys
    alt not your bot, or retired
        S-->>P: 404 not_found
    else
        S->>DB: new_key: optcg_ + 8 hex prefix + _ + random, store prefix + SHA-256
        S-->>P: {key} shown once
    end
    U->>P: revoke a key
    P->>S: DELETE /api/keys/{key id}
    S->>DB: set revoked where the key's bot is yours
    S-->>P: {ok} or 404
    U->>P: retire the bot
    P->>S: DELETE /api/bots/{id}
    S->>DB: revoke all its keys, set retired, rename to name#id (frees the name)
    S-->>P: {ok}
```

Only a key's hash is stored, so a lost key can't be shown again; make a new one. A retired bot's
matches stay in the records.

## Deck building

The deck builder runs in the browser and keeps decks in `localStorage`; the server only checks a
deck when a game is started with it.

```mermaid
sequenceDiagram
    actor U as Person
    participant B as Deck builder (React)
    participant LS as localStorage "optcg.decks"
    participant S as Server
    participant DB as carddb, deck.py
    B->>S: GET /api/cards
    S-->>B: every card (cached after the first request)
    B->>S: GET /api/options
    B->>LS: loadDecks()
    alt storage blocked (private window)
        LS-->>B: [] and saving reports it can't keep them
    end
    opt start from a site deck
        U->>B: pick a site deck
        B->>S: GET /api/deck?name=...
        alt unknown name
            S-->>B: 404 not_found
        else
            S-->>B: {leader, cards}
        end
    end
    opt paste a list
        U->>B: paste "4xOP01-016" lines
        B->>B: parseList: unknown line, unknown card, not exactly one Leader are shown as errors
    end
    U->>B: add and remove cards
    B->>B: problems(): Leader chosen, 50 cards, colors shared, copies
    B->>LS: saveDecks()
    U->>B: Play this deck
    B->>B: /play?deck=built:id
    U->>S: POST /api/games {deck: its name, decklist: "1xLEADER, Nx id ..."}
    S->>DB: parse_decklist, validate
    alt the deck breaks a rule
        S-->>U: 400 bad_deck "your deck can't be played: ..."
    else
        S-->>U: {url: "/game"} (as in a browser game)
    end
```

The Play page won't start with an unfinished built deck (fewer or more than 50 cards); the
server checks the deck rules again either way.

## Accounts

- `GET /api/account`: the user, totals and the last 20 browser games (`win`, `loss` or `draw`).
- `PATCH /api/account {name}`: 1 to 40 characters after trimming, else 400.
- `DELETE /api/account`: deletes the user's matches, keys, bots, games and the user, deletes the
  replay files of those matches, and clears the session.

## Configuration

Every setting is an environment variable read by `Config.from_env()`; `.env.example` has them all.
Values are never in this documentation.

| Variable | Default | Meaning |
|---|---|---|
| `OPTCG_BOTS` | `greedy` | House bots, `;`-separated, each `Name=spec` or a spec; the first is the default opponent |
| `OPTCG_DECKS` | lists in `decks/` + every official deck | Decks offered, `;`-separated names |
| `OPTCG_HOUSE_DECK` | `(Yellow) Charlotte Linlin` | The house bot's deck unless another is asked for |
| `OPTCG_DB` | `server.db` (`/data/server.db` in Docker) | SQLite file |
| `OPTCG_REPLAYS` | `replays/server` (`/data/replays` in Docker) | Bot match replays |
| `OPTCG_SECRET` | random per start | Signs the session cookie |
| `OPTCG_SECURE_COOKIES` | off (`1` in docker compose) | Cookie over HTTPS only |
| `OPTCG_PUBLIC_URL` | request URL | Where Google redirects back to |
| `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET` | unset | Google sign-in; both needed |
| `OPTCG_DEV_LOGIN` | off | `1` enables `/login/dev`. Never on a public server |
| `OPTCG_ART_BASE` | `art` (served from `StreamingAssets/`) | Base URL for card art, e.g. a CloudFront URL |
| `OPTCG_ART_MANIFEST` | unset | File listing the art files when art is not local |
| `OPTCG_MOVE_TIMEOUT` | 30 | Seconds a remote bot gets per decision |
| `OPTCG_MAX_GAMES` | 500 | Live browser games |
| `OPTCG_IDLE_MINUTES` | 30 | Idle browser games are dropped after this |
| `OPTCG_BOT_MATCHES` | 2 | Matches one remote bot may play at once |
| `HOST`, `PORT` | `127.0.0.1`, 8000 | Where uvicorn listens |
| `OPTCG_DOMAIN` | | Used by Caddy and `deploy.sh`, not the app |
