# Database

The server keeps its durable data in one SQLite file (`OPTCG_DB`, `/data/server.db` in Docker),
through `optcg/server/store.py`. The schema is created on startup (`create table if not
exists`), and one migration adds `bots.retired` to databases made before bots could be retired.

Live browser games are not in the database (they are in memory, see
[Web server](server.md#components-and-state)), and bot match replays are JSON files in
`OPTCG_REPLAYS`, named by match id.

## ER diagram

```mermaid
erDiagram
    users ||--o{ bots : "owns (bots.user_id)"
    bots ||--o{ keys : "has (keys.bot_id)"
    bots ||--o{ matches : "plays (matches.bot_id)"
    users |o--o{ games : "plays in the browser (games.user_id, empty for guests)"

    users {
        integer id PK
        text sub UK "google:<sub> or dev:<name>, not null"
        text email
        text name
        real created "unix time, not null"
    }
    bots {
        integer id PK
        integer user_id FK "not null"
        text name UK "3 to 32 of A-Z a-z 0-9 - _, not null"
        real created "not null"
        real retired "unix time, null while active"
    }
    keys {
        integer id PK
        integer bot_id FK "not null"
        text prefix UK "8 hex characters, not null"
        text hash "SHA-256 of the whole key, not null"
        real created "not null"
        real revoked "unix time, null while valid"
    }
    matches {
        text id PK "16 hex characters"
        integer bot_id FK "not null"
        text opponent "house bot name, not null"
        integer seat "0 or 1, the remote bot's seat"
        text decks "JSON list of the two Leader names, by seat"
        integer seed "the game's seed, not null"
        real started "not null"
        real ended "null while playing"
        integer winner "0, 1, or null for a draw or unfinished"
        text reason "normal, illegal_move, timeout, concede, disconnect"
        integer turns
    }
    games {
        integer id PK
        integer user_id FK "null for guests"
        text bot "house bot name, not null"
        text deck "the person's deck name, not null"
        text bot_deck "not null"
        integer seat "the person's seat, not null"
        integer winner "0, 1, or null for a draw"
        text reason "normal or concede, not null"
        integer turns "not null"
        real ended "not null"
    }
```

All times are Unix seconds (`time.time()`). Foreign keys are declared with `references` but
SQLite doesn't enforce them unless `pragma foreign_keys` is on, which the store doesn't set;
`delete_user` removes the rows in dependency order itself.

## Tables

### `users`

One row per person who has signed in. `sub` is Google's id for the person (prefixed `google:`),
which stays the same when their email changes; dev sign-in uses `dev:<name>`.
`Store.user(sub, email, name)` inserts, or on a known `sub` updates `email` and `name`.
`rename` sets `name` (1 to 40 characters).

### `bots`

A remote bot registered by a user. `name` is unique across all accounts. At most 10 active bots
per account (`MAX_BOTS`). Retiring (`delete_bot`) sets `retired`, revokes the bot's keys and
renames it to `name#id`, so the name can be taken again while its matches stay on record.
`Store.bots(user_id)` lists only active bots.

### `keys`

API keys for a bot. A key is `optcg_<prefix>_<random>`: `prefix` is 8 hex characters (stored,
unique, used to look the key up) and the random part is 32 URL-safe characters. Only the
SHA-256 of the whole key is kept, so a key is shown once, when it is made.

```mermaid
flowchart LR
    k["Authorization: Bearer optcg_1a2b3c4d_..."] --> re{"matches<br/>optcg_(8 hex)_(20 to 64 URL-safe)?"}
    re -- no --> none["None: unauthorized"]
    re -- yes --> q["select hash and the bot<br/>from keys join bots<br/>where prefix = ? and revoked is null"]
    q --> found{"row found?"}
    found -- no --> none
    found -- yes --> cmp{"hmac.compare_digest(row.hash,<br/>sha256(key))"}
    cmp -- no --> none
    cmp -- yes --> bot["the bot row"]
```

Revoking (`revoke`) sets `revoked`, only for a key whose bot belongs to the user asking.

### `matches`

One row per bot API match, written when the match starts (`start_match`) and completed when it
ends (`end_match`: `ended`, `winner`, `reason`, `turns`). `seat` is the remote bot's seat, so the
bot won when `winner = seat`. `decks` holds the Leader names, not the decklists; `seed` with the
replay file reproduces the game. A match whose server stopped mid-game keeps `ended` null.

### `games`

One row per finished browser game (`add_game`), written once when the game ends normally or by
concession. `seat` is the person's seat. Guests' games have no `user_id`; they still count in
the house bots' record.

## Queries the site runs

| Store method | Used by | What it reads |
|---|---|---|
| `record(bot_id)` | `GET /api/bots` | Finished matches and wins (`winner = seat`) for a bot |
| `matches(user_id, 20)` | `GET /api/bots` | The user's bots' last 20 matches, newest first |
| `games(user_id, 20)`, `game_totals` | `GET /api/account` | The user's last 20 browser games and totals |
| `house_record()` | `GET /api/analysis` | Per house bot: games and wins against people (`games`), and against remote bots (finished `matches`) |
| `match(id)` | tests only (`/matches/{id}` reads the replay file instead) | A match with its bot's name |

## Deleting an account

```mermaid
sequenceDiagram
    participant S as DELETE /api/account
    participant DB as Store.delete_user (one transaction)
    participant F as Replay files
    S->>DB: delete_user(uid)
    DB->>DB: ids = matches of the user's bots
    DB->>DB: delete matches, keys, bots of the user
    DB->>DB: delete games, then the user
    DB-->>S: match ids
    S->>F: delete id.json for each match id
    S->>S: clear the session cookie
```

## Concurrency

The store uses one `sqlite3` connection shared across threads (`check_same_thread=False`) behind
a `threading.Lock`; every statement runs inside `with self.lock, self.db`, which also commits.
That is enough for one app process, which is how the site runs.

## Backups

`/data` (the `data` Docker volume) holds `server.db` and the replays. Copy it off the server from
time to time (see [`docs/hosting.md`](../hosting.md#backups)).
