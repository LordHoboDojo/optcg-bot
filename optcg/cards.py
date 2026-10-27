"""Card definitions (printed data) and card instances (a physical card in a game).

Rule references like (2-10) point at the Comprehensive Rules in rules/.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


class Category(str, Enum):
    LEADER = "Leader"
    CHARACTER = "Character"
    EVENT = "Event"
    STAGE = "Stage"


class Timing(str, Enum):
    ON_PLAY = "On Play"
    WHEN_ATTACKING = "When Attacking"
    ON_OPP_ATTACK = "On Your Opponent's Attack"
    ON_BLOCK = "On Block"
    ON_KO = "On K.O."
    END_OF_YOUR_TURN = "End of Your Turn"
    END_OF_OPP_TURN = "End of Your Opponent's Turn"
    ACTIVATE_MAIN = "Activate: Main"
    MAIN = "Main"          # Event cards only (10-2-3)
    COUNTER = "Counter"    # Event cards only (10-2-4)
    TRIGGER = "Trigger"    # activated from Life on damage (10-1-5)
    START_OF_GAME = "Start of Game"
    START_OF_TURN = "Start of Turn"       # 6-2-2: "at the start of your/your opponent's turn"
    START_OF_MAIN = "Start of Main Phase"  # 6-5-1
    END_OF_BATTLE = "End of Battle"        # 7-1-5-2: "at the end of the/this battle"
    # Engine events with no printed keyword (fired for every card on the field):
    ATTACK_DECLARED = "Attack Declared"   # after [When Attacking] / [On Your Opponent's Attack]
    DON_RETURNED = "DON!! Returned"       # after a DON!! -X cost returned DON!! to the DON!! deck


# Keyword effects (10-1). [Trigger] is modelled as an Effect instead.
RUSH = "Rush"
RUSH_CHARACTER = "Rush: Character"
DOUBLE_ATTACK = "Double Attack"
BANISH = "Banish"
BLOCKER = "Blocker"
UNBLOCKABLE = "Unblockable"
KEYWORDS = frozenset({RUSH, RUSH_CHARACTER, DOUBLE_ATTACK, BANISH, BLOCKER, UNBLOCKABLE})
# Not printed keywords: markers that permanent or temporary effects grant (10-2-1-3).
CANNOT_ATTACK = "Cannot Attack"
NO_KO_IN_BATTLE = "Cannot be K.O.'d in battle"
NO_KO_BY_EFFECTS = "Cannot be K.O.'d by effects"
NO_KO_BY_OPP_EFFECTS = "Cannot be K.O.'d by your opponent's effects"

COLORS = frozenset({"red", "green", "blue", "purple", "black", "yellow"})


@dataclass(frozen=True)
class Effect:
    """One effect printed on a card.

    `resolve(game, source)` does the work. It may be a plain function or a
    generator that yields from game helpers which ask players to choose.
    """
    timing: Timing
    resolve: Callable
    text: str = ""
    once_per_turn: bool = False
    don_req: int = 0            # [DON!! xX] condition (8-3-2-3)
    your_turn: bool = False     # [Your Turn]
    opp_turn: bool = False      # [Opponent's Turn]
    rest_don: int = 0           # activation cost: rest N active DON!! (the circled-number symbol, 8-3-1-5)
    don_minus: int = 0          # activation cost: DON!! -X (8-3-1-6)
    rest_self: bool = False     # activation cost: rest this card
    condition: Optional[Callable] = None  # (game, source) -> bool
    optional: bool = False      # "you may": the owner is asked before activating
    # Effects printed as one ("[When Attacking]/[On Your Opponent's Attack]") share [Once Per Turn].
    opt_group: Optional[str] = None


@dataclass(frozen=True)
class Replacement:
    """A replacement effect (8-1-3-4) that stops a card leaving the field.

    applies(game, source, card, by, ko): `card` would be removed; `by` is the player whose
    effect removes it (None for battle and rules), `ko` whether it would be K.O.'d.
    pay(game, source, card) is a generator that does the "instead" part.
    """
    applies: Callable
    pay: Callable
    payable: Callable = lambda g, src: True
    text: str = ""


@dataclass(frozen=True, eq=False)
class CardDef:
    id: str
    name: str
    category: Category
    colors: tuple[str, ...]
    cost: int = 0
    power: int = 0
    counter: int = 0
    life: int = 0
    types: tuple[str, ...] = ()
    attributes: tuple[str, ...] = ()
    keywords: frozenset[str] = frozenset()
    effects: tuple[Effect, ...] = ()
    # Permanent effect that changes other cards' power while this card is on the field:
    # (game, source, card) -> power delta.
    static_power: Optional[Callable] = None
    # Same, for keywords: (game, source, card) -> set of keywords granted to `card`.
    static_keywords: Optional[Callable] = None
    # Same, for cost: (game, source, card) -> cost delta.
    static_cost: Optional[Callable] = None
    # Same, setting base power: (game, source, card) -> new base power or None.
    static_base_power: Optional[Callable] = None
    # Same, for Counter values of cards in hand: (game, source, card) -> Counter or None.
    static_counter: Optional[Callable] = None
    # While this card is in hand: (game, card) -> cost delta, and (game, card) -> Counter or None.
    hand_cost: Optional[Callable] = None
    hand_counter: Optional[Callable] = None
    # While on the field: (game, source) -> True if the opponent may only attack cards with this name.
    attack_lock: Optional[Callable] = None
    replacements: tuple[Replacement, ...] = ()
    text: str = ""
    trigger_text: str = ""
    # Effect lines from the printed text that the engine does not carry out yet.
    unimplemented: tuple[str, ...] = ()

    def __post_init__(self):
        bad = set(self.colors) - COLORS
        if bad:
            raise ValueError(f"{self.id}: unknown colors {bad}")
        bad = set(self.keywords) - KEYWORDS
        if bad:
            raise ValueError(f"{self.id}: unknown keywords {bad}")

    def effect_indexes(self, timing: Timing) -> list[int]:
        return [i for i, e in enumerate(self.effects) if e.timing == timing]


@dataclass
class Mod:
    """A temporary change applied by an effect."""
    kind: str        # "power" | "base_power" | "cost" | "keyword" | "no_refresh" | "negated"
    value: object
    # "battle" | "turn" | "next_turn" (start of `player`'s next turn)
    # | "next_end" (end of the next turn that isn't `player`'s) | "permanent"
    until: str
    player: int      # whose effect it is (for "until the start of your next turn")


class Card:
    """A physical card in a game. `incarnation` changes every time the card
    changes zones, because a moved card counts as a new card (3-1-6)."""

    def __init__(self, uid: int, definition: CardDef, owner: int):
        self.uid = uid
        self.d = definition
        self.owner = owner
        self.zone: Optional[str] = None
        self.face_up = False  # a Life card placed face-up is open information (3-10-2-1)
        self.incarnation = 0
        self.rested = False
        self.don = 0                 # DON!! cards given to this card
        self.played_turn = -1
        self.mods: list[Mod] = []
        self.opt_used: dict[int, int] = {}  # effect index -> turn number ([Once Per Turn])

    @property
    def name(self) -> str:
        return self.d.name

    @property
    def category(self) -> Category:
        return self.d.category

    def reset(self) -> None:
        self.incarnation += 1
        self.face_up = False
        self.rested = False
        self.played_turn = -1
        self.mods = []
        self.opt_used = {}

    def __repr__(self) -> str:
        return f"<{self.d.name}#{self.uid} p{self.owner} {self.zone}>"
