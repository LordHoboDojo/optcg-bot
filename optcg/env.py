"""A training environment: one seat is the learner, the other is a fixed opponent agent.

    env = Env(find_deck("Rocks"), find_deck("Linlin"), opponent=GreedyAgent())
    obs = env.reset(seed=0)
    while not env.done:
        obs, reward, done = env.step(pick(obs))   # index into obs["options"]

Observations hold only what the learner may see (3-1-4, 3-1-5) and are plain lists of
numbers, so any ML library can turn them into tensors:

    obs["scalars"]  fixed-length game-state numbers (Life, hand, DON!!, turn, ...)
    obs["cards"]    one row per visible card: CARD_FIELDS, card ids from `card_index()`
    obs["options"]  one row per legal option: OPTION_FIELDS
    obs["decision"] index of the decision kind in DECISION_KINDS

Options vary in number and meaning at every decision, so a policy should score each option
row (together with the state) and pick among them, not use a fixed action space.
Reward is +1 for a win, -1 for a loss and 0 otherwise (draws and games cut off at max_steps).

`engine="rust"` runs the game in the Rust engine (engine-rs/, module optcg_rs), which plays the
same games and gives the same observations, several times faster. Agents then answer through
`pick(game)` instead of `act(game, decision)`.
"""
from __future__ import annotations

import random
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .actions import Decision
from .agents import Agent
from .cards import BLOCKER, DOUBLE_ATTACK, BANISH, RUSH, UNBLOCKABLE, Card, Category
from .deck import Deck
from .engine import Game

DECISION_KINDS = ("main", "block", "counter", "trigger", "select", "confirm", "choose_one", "cost",
                  "go_first", "mulligan", "order", "order_cards", "return_don")
ACTION_KINDS = ("play", "activate", "give_don", "attack", "end", "block", "counter", "event", "pass",
                "select", "yes", "no", "first", "second", "keep", "mulligan")
ZONES = ("hand", "leader", "character", "stage", "trash", "life", "deck")
CATEGORIES = tuple(Category)
PHASES = ("setup", "refresh", "draw", "don", "main", "attack", "end_of_battle", "end")
KEYWORD_FIELDS = (RUSH, DOUBLE_ATTACK, BANISH, BLOCKER, UNBLOCKABLE)

# per-card row: card id, mine (1) or the opponent's (0), zone, then numbers that change in play
CARD_FIELDS = ("card", "mine", "zone", "category", "power", "cost", "counter", "rested", "don",
               "played_this_turn", "in_battle") + KEYWORD_FIELDS
# per-option row: action kind, the acting card and the target described by their row in obs["cards"]
OPTION_FIELDS = ("kind", "card_row", "target_row", "index", "card_id", "target_id")
SCALAR_FIELDS = tuple(f"{side}_{k}" for side in ("me", "opp") for k in (
    "life", "hand", "deck", "trash", "don_active", "don_rested", "don_deck", "characters")) + (
    "my_turn", "turn", "phase", "battle")

POWER_SCALE = 1000  # powers and counters are given in thousands


@lru_cache(maxsize=1)
def card_index() -> dict[str, int]:
    """Card id -> embedding index for every known card. 0 means no card / unknown."""
    from .carddb import load_cards
    from .cardpool import GREEN_DECK, RED_DECK
    samples = {d.id for deck in (RED_DECK, GREEN_DECK) for d in (deck.leader, *deck.cards)}
    ids = sorted(set(load_cards()) | samples)
    return {cid: i + 1 for i, cid in enumerate(ids)}


def _index(lst: tuple, value) -> int:
    return lst.index(value) if value in lst else len(lst)


def encode(g: Game, d: Decision) -> dict:
    """The observation for the player who must answer `d`."""
    me = d.player
    ids = card_index()
    rows: list[list[float]] = []
    row_of: dict[int, int] = {}
    battle = {g.battle.attacker.uid, g.battle.target.uid} if g.battle else set()

    def add(c: Card, zone: str, open_stats: bool = True) -> None:
        on_field = zone in ("leader", "character", "stage")
        powered = c.category in (Category.LEADER, Category.CHARACTER)
        row = [ids.get(c.d.id, 0), float(c.owner == me), _index(ZONES, zone), _index(CATEGORIES, c.category),
               (g.power(c) if on_field and powered else c.d.power or 0) / POWER_SCALE,
               g.cost(c) if c.category != Category.LEADER else 0,
               (g.counter_value(c) if zone == "hand" else c.d.counter or 0) / POWER_SCALE,
               float(c.rested), c.don, float(c.played_turn == g.turn_number and on_field),
               float(c.uid in battle)]
        row += [float(on_field and g.has_keyword(c, k)) for k in KEYWORD_FIELDS]
        row_of[c.uid] = len(rows)
        rows.append(row)

    for p in (me, 1 - me):
        ps = g.players[p]
        add(ps.leader, "leader")
        for c in ps.characters:
            add(c, "character")
        if ps.stage:
            add(ps.stage, "stage")
        for c in ps.trash:
            add(c, "trash")
        for c in ps.life:
            if c.face_up:
                add(c, "life")
        if p == me:
            for c in ps.hand:
                add(c, "hand")

    # cards an effect lets this player see (deck searches, revealed cards) appear only in options
    for o in d.options:
        for uid in (o.card, o.target):
            if uid is not None and uid not in row_of:
                c = g.cards[uid]
                add(c, c.zone if c.zone in ZONES else "deck")

    options = []
    for o in d.options:
        card = g.cards[o.card] if o.card is not None else None
        target = g.cards[o.target] if o.target is not None else None
        options.append([_index(ACTION_KINDS, o.kind),
                        row_of[o.card] if card else -1, row_of[o.target] if target else -1,
                        o.index if o.index is not None else -1,
                        ids.get(card.d.id, 0) if card else 0, ids.get(target.d.id, 0) if target else 0])

    scalars: list[float] = []
    for p in (me, 1 - me):
        ps = g.players[p]
        scalars += [len(ps.life), len(ps.hand), len(ps.deck), len(ps.trash),
                    ps.don_active, ps.don_rested, ps.don_deck, len(ps.characters)]
    scalars += [float(g.turn_player == me), g.turn_number, _index(PHASES, g.phase), float(g.battle is not None)]

    return {"scalars": scalars, "cards": rows, "options": options,
            "decision": _index(DECISION_KINDS, d.kind), "player": me}


# ---------------------------------------------------------------- the Rust engine

def rust_available() -> bool:
    try:
        import optcg_rs  # noqa: F401
    except ImportError:
        return False
    return True


@lru_cache(maxsize=1)
def rust_db():
    """The Rust engine's card database, numbered like card_index() for encoding."""
    import optcg_rs
    return optcg_rs.Db(str(Path(__file__).resolve().parent.parent / "data" / "cards.json"), card_index())


def rust_game(decks: tuple[Deck, Deck], seed: Optional[int] = None, fast: bool = True):
    """A Rust game with these decks. `fast` turns off log lines and option labels."""
    import optcg_rs
    g = optcg_rs.Game(rust_db(), [(d.leader.id, [c.id for c in d.cards]) for d in decks],
                      random.randrange(1 << 62) if seed is None else seed)
    if fast:
        g.logging = False
        g.labels = False
    return g


def encode_rust(g) -> dict:
    """encode() for a Rust game's pending decision; the same numbers."""
    scalars, cards, options, decision, player = g.encode()
    return {"scalars": scalars, "cards": cards, "options": options, "decision": decision, "player": player}


def play_rust(decks: tuple[Deck, Deck], agents: tuple[Agent, Agent], seed: Optional[int] = None,
              max_steps: int = 20_000):
    """play_game() on the Rust engine; returns the finished game."""
    g = rust_game(decks, seed)
    for _ in range(max_steps):
        if g.over:
            return g
        g.step(agents[g.pending_player()].pick(g))
    raise RuntimeError(f"game did not finish in {max_steps} steps")


class Env:
    """The learner plays `seat`; `opponent` answers the other seat's decisions inside step().

    With opponent=None the caller answers both seats: step() then stops at every decision, and
    `to_move` says whose it is (for batching several games' network calls together)."""

    def __init__(self, deck0: Deck, deck1: Deck, opponent: Optional[Agent], seat: int = 0, max_steps: int = 20_000,
                 engine: str = "python"):
        if engine not in ("python", "rust"):
            raise ValueError(f"unknown engine {engine!r}")
        self.decks = (deck0, deck1)
        self.opponent, self.seat, self.max_steps = opponent, seat, max_steps
        self.rust = engine == "rust"
        self.game: Optional[Game] = None
        self.steps = 0
        self.truncated = False

    @property
    def done(self) -> bool:
        return self.game is None or self.game.over or self.truncated

    @property
    def to_move(self) -> int:
        """The player who must answer the pending decision."""
        return self.game.pending_player() if self.rust else self.game.pending.player

    @property
    def decision(self) -> Decision:
        """The pending Decision (Python engine only)."""
        return self.game.pending

    def _obs(self) -> dict:
        return encode_rust(self.game) if self.rust else encode(self.game, self.game.pending)

    def reset(self, seed: Optional[int] = None) -> Optional[dict]:
        self.game = rust_game(self.decks, seed) if self.rust else Game(self.decks, seed=seed)
        self.steps = 0
        self.truncated = False
        self._advance()
        return None if self.done else self._obs()

    def step(self, option: int) -> tuple[Optional[dict], float, bool]:
        """Answer the current decision with option number `option`; returns (obs, reward, done).
        `obs` is for whoever decides next: the learner, or with opponent=None either seat."""
        if self.done:
            raise RuntimeError("the game is over; call reset()")
        self.game.step(option if self.rust else self.game.pending.options[option])
        self.steps += 1
        self._advance()
        if self.done:
            return None, self.reward(), True
        return self._obs(), 0.0, False

    def reward(self) -> float:
        g = self.game
        if not g.over or g.winner is None:
            return 0.0
        return 1.0 if g.winner == self.seat else -1.0

    def _advance(self) -> None:
        """Let the opponent play until the learner must decide or the game ends."""
        g = self.game
        while not g.over:
            if self.steps >= self.max_steps:
                self.truncated = True
                return
            if self.opponent is None:
                return
            if self.rust:
                if g.pending_player() == self.seat:
                    return
                g.step(self.opponent.pick(g))
            else:
                d = g.pending
                if d.player == self.seat:
                    return
                g.step(self.opponent.act(g, d))
            self.steps += 1
