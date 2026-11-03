"""Server settings, read from environment variables (see .env.example)."""
from __future__ import annotations

import json
import os
import secrets
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from ..deck import Deck
from ..play import DECK_DIR, resolve_deck


def _list(name: str, default: list[str]) -> list[str]:
    """A ;-separated list (deck names can hold commas)."""
    raw = os.environ.get(name, "").strip()
    return [x.strip() for x in raw.split(";") if x.strip()] if raw else default


@dataclass
class Config:
    bots: list[str] = field(default_factory=lambda: ["greedy"])  # house bots; the first is the default
    decks: Optional[list[str]] = None  # decks to offer; None means your lists in decks/ and every official deck
    house_deck: str = "(Yellow) Charlotte Linlin"  # the house bot's deck unless one is asked for
    db: str = "server.db"
    replays: Path = Path("replays/server")
    secret: str = ""  # signs the session cookie; a random one (sessions end on restart) if empty
    secure_cookies: bool = False  # send the cookie over HTTPS only
    public_url: str = ""  # e.g. https://optcg.example.com, for Google's redirect back to us
    google_id: str = ""
    google_secret: str = ""
    dev_login: bool = False  # /login/dev?name=... signs in without Google. Never turn this on in public.
    art_base: str = "art"  # where the page loads card art from: "art" (served from StreamingAssets) or a URL
    art_manifest: Optional[Path] = None  # list of art files, for art kept somewhere other than StreamingAssets
    move_timeout: float = 30.0  # seconds a remote bot gets for each decision
    max_games: int = 500  # live browser games
    idle_minutes: float = 30.0  # a browser game left alone this long is dropped
    bot_matches: int = 2  # matches one remote bot may play at once

    @classmethod
    def from_env(cls) -> "Config":
        e = os.environ.get
        return cls(bots=_list("OPTCG_BOTS", ["greedy"]), decks=_list("OPTCG_DECKS", []) or None,
                   house_deck=e("OPTCG_HOUSE_DECK", cls.house_deck), db=e("OPTCG_DB", cls.db),
                   replays=Path(e("OPTCG_REPLAYS", str(cls.replays))), secret=e("OPTCG_SECRET", ""),
                   secure_cookies=e("OPTCG_SECURE_COOKIES") == "1", public_url=e("OPTCG_PUBLIC_URL", "").rstrip("/"),
                   google_id=e("GOOGLE_CLIENT_ID", ""), google_secret=e("GOOGLE_CLIENT_SECRET", ""),
                   dev_login=e("OPTCG_DEV_LOGIN") == "1", art_base=e("OPTCG_ART_BASE", "art").rstrip("/"),
                   art_manifest=Path(e("OPTCG_ART_MANIFEST")) if e("OPTCG_ART_MANIFEST") else None,
                   move_timeout=float(e("OPTCG_MOVE_TIMEOUT", cls.move_timeout)),
                   max_games=int(e("OPTCG_MAX_GAMES", cls.max_games)),
                   idle_minutes=float(e("OPTCG_IDLE_MINUTES", cls.idle_minutes)),
                   bot_matches=int(e("OPTCG_BOT_MATCHES", cls.bot_matches)))

    @property
    def google(self) -> bool:
        return bool(self.google_id and self.google_secret)

    def session_secret(self) -> str:
        if not self.secret:
            self.secret = secrets.token_urlsafe(32)
        return self.secret

    def art_files(self) -> Optional[tuple[str, ...]]:
        """The art file list from the manifest, or None to look in StreamingAssets."""
        return tuple(json.loads(self.art_manifest.read_text())) if self.art_manifest else None

    def load_decks(self) -> dict[str, Deck]:
        """The decks people and bots may pick, by name. Only these names are ever looked up, so a
        name sent to the server can't point at a file."""
        if self.decks is None:
            from ..carddb import official_decks
            out = {p.stem: resolve_deck(p.stem) for p in sorted(DECK_DIR.glob("*.txt"))}
            return {**out, **official_decks()}
        return {n: resolve_deck(n) for n in self.decks}
