"""Accounts, bots, API keys and bot matches, kept in SQLite."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from typing import Optional

SCHEMA = """
create table if not exists users (id integer primary key, sub text unique not null, email text, name text,
                                  created real not null);
create table if not exists bots (id integer primary key, user_id integer not null references users(id),
                                 name text unique not null, created real not null);
create table if not exists keys (id integer primary key, bot_id integer not null references bots(id),
                                 prefix text unique not null, hash text not null, created real not null, revoked real);
create table if not exists matches (id text primary key, bot_id integer not null references bots(id),
                                    opponent text not null, seat integer not null, decks text not null,
                                    seed integer not null, started real not null, ended real,
                                    winner integer, reason text, turns integer);
"""

BOT_NAME = re.compile(r"[A-Za-z0-9_-]{3,32}")
KEY = re.compile(r"optcg_([0-9a-f]{8})_[A-Za-z0-9_-]{20,64}")
MAX_BOTS = 10  # per account


def _hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


class Store:
    def __init__(self, path: str = ":memory:"):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock, self.db:
            self.db.executescript(SCHEMA)

    def _all(self, sql: str, args=()) -> list[sqlite3.Row]:
        with self.lock, self.db:
            return self.db.execute(sql, args).fetchall()

    def _one(self, sql: str, args=()) -> Optional[sqlite3.Row]:
        rows = self._all(sql, args)
        return rows[0] if rows else None

    def _insert(self, sql: str, args=()) -> int:
        with self.lock, self.db:
            return self.db.execute(sql, args).lastrowid

    # People sign in with Google; `sub` is Google's id for them, which stays the same when their email changes.
    def user(self, sub: str, email: Optional[str], name: Optional[str]) -> int:
        self._insert("insert into users (sub, email, name, created) values (?, ?, ?, ?) "
                     "on conflict(sub) do update set email = excluded.email, name = excluded.name",
                     (sub, email, name, time.time()))
        return self._one("select id from users where sub = ?", (sub,))["id"]

    def get_user(self, uid: int) -> Optional[sqlite3.Row]:
        return self._one("select * from users where id = ?", (uid,))

    def add_bot(self, user_id: int, name: str) -> int:
        if not BOT_NAME.fullmatch(name):
            raise ValueError("a bot's name is 3 to 32 letters, digits, - or _")
        if len(self.bots(user_id)) >= MAX_BOTS:
            raise ValueError(f"an account can have at most {MAX_BOTS} bots")
        try:
            return self._insert("insert into bots (user_id, name, created) values (?, ?, ?)", (user_id, name, time.time()))
        except sqlite3.IntegrityError:
            raise ValueError(f"the name {name!r} is taken") from None

    def bots(self, user_id: int) -> list[sqlite3.Row]:
        return self._all("select * from bots where user_id = ? order by id", (user_id,))

    def bot(self, bot_id: int) -> Optional[sqlite3.Row]:
        return self._one("select * from bots where id = ?", (bot_id,))

    def new_key(self, bot_id: int) -> str:
        """A new API key for a bot. Only its hash is kept, so this is the one time it can be shown."""
        prefix = secrets.token_hex(4)
        key = f"optcg_{prefix}_{secrets.token_urlsafe(24)}"
        self._insert("insert into keys (bot_id, prefix, hash, created) values (?, ?, ?, ?)",
                     (bot_id, prefix, _hash(key), time.time()))
        return key

    def keys(self, bot_id: int) -> list[sqlite3.Row]:
        return self._all("select id, prefix, created from keys where bot_id = ? and revoked is null order by id", (bot_id,))

    def revoke(self, key_id: int, user_id: int) -> bool:
        with self.lock, self.db:
            return self.db.execute(
                "update keys set revoked = ? where id = ? and revoked is null "
                "and bot_id in (select id from bots where user_id = ?)", (time.time(), key_id, user_id)).rowcount > 0

    def bot_for_key(self, key: str) -> Optional[sqlite3.Row]:
        m = KEY.fullmatch(key)
        if not m:
            return None
        row = self._one("select k.hash, b.* from keys k join bots b on b.id = k.bot_id "
                        "where k.prefix = ? and k.revoked is null", (m.group(1),))
        return row if row and hmac.compare_digest(row["hash"], _hash(key)) else None

    def start_match(self, match_id: str, bot_id: int, opponent: str, seat: int, decks: list[str], seed: int) -> None:
        self._insert("insert into matches (id, bot_id, opponent, seat, decks, seed, started) values (?, ?, ?, ?, ?, ?, ?)",
                     (match_id, bot_id, opponent, seat, json.dumps(decks), seed, time.time()))

    def end_match(self, match_id: str, winner: Optional[int], reason: str, turns: int) -> None:
        self._insert("update matches set ended = ?, winner = ?, reason = ?, turns = ? where id = ?",
                     (time.time(), winner, reason, turns, match_id))

    def match(self, match_id: str) -> Optional[sqlite3.Row]:
        return self._one("select m.*, b.name as bot from matches m join bots b on b.id = m.bot_id where m.id = ?",
                         (match_id,))

    def matches(self, user_id: int, limit: int = 20) -> list[sqlite3.Row]:
        return self._all("select m.*, b.name as bot from matches m join bots b on b.id = m.bot_id "
                         "where b.user_id = ? order by m.started desc limit ?", (user_id, limit))
