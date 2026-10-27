"""Hand-written effects for real cards whose text the compiler can't handle.

    @script("OP17-020")
    def shanks_leader():
        return {"effects": (Effect(...),)}

A script returns CardDef fields to set (effects, replacements, hand_counter, ...).
With `covers`, only the unimplemented lines containing those strings are replaced and
returned effects are added to the compiled ones; without it, the script replaces all of
the card's effects. Keywords printed on the card are always read from the text.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

from .actions import Action
from .cards import Category, Effect, Replacement, Timing


@dataclass(frozen=True)
class Script:
    build: Callable[[], dict]
    covers: Optional[tuple[str, ...]]


SCRIPTS: dict[str, Script] = {}


def script(card_id: str, covers: str | tuple[str, ...] | None = None):
    def register(fn):
        SCRIPTS[card_id] = Script(fn, (covers,) if isinstance(covers, str) else covers)
        return fn
    return register


def _types_include(card, text):
    return any(text in t for t in card.d.types)


def _leader(g, src):
    return g.players[src.owner].leader


# ---------------------------------------------------------------- OP-17 Leaders

@script("OP17-020")
def shanks_leader():
    """[Activate: Main] [Once Per Turn] Trash 1 card or rest 1 DON!!: up to 1 of your opponent's
    rested Characters won't become active in their next Refresh Phase."""
    def payable(g, src):
        ps = g.players[src.owner]
        return bool(ps.hand) or ps.don_active > 0

    def run(g, src):
        ps = g.players[src.owner]
        opts = []
        if ps.hand:
            opts.append(Action("select", index=0, label="Trash 1 card from your hand"))
        if ps.don_active:
            opts.append(Action("select", index=1, label="Rest 1 of your DON!! cards"))
        a = yield from g.choose(src.owner, "cost", opts, "Pay Shanks's cost")
        if a.index == 0:
            yield from g.trash_from_hand(src.owner, 1)
        else:
            ps.don_active -= 1
            ps.don_rested += 1
        targets = [c for c in g.characters(1 - src.owner) if c.rested]
        for c in (yield from g.choose_cards(src.owner, targets, 1, "Won't become active next Refresh Phase")):
            g.skip_next_refresh(c, by=src.owner)

    return {"effects": (Effect(Timing.ACTIVATE_MAIN, run, "Keep an opponent's rested Character rested",
                               once_per_turn=True, condition=payable),)}


@script("OP17-039")
def xebec_leader():
    """[When Attacking] You may trash 1 card: reveal the top card of your deck; if its type
    includes "Rocks Pirates", draw 2 cards."""
    def run(g, src):
        ps = g.players[src.owner]
        if not (yield from g.confirm(src.owner, "Trash 1 card to reveal the top card of your deck?")):
            return
        yield from g.trash_from_hand(src.owner, 1)
        if not ps.deck:
            return
        top = ps.deck[-1]
        g._log(f"Player {src.owner} reveals {top.name}")
        if _types_include(top, "Rocks Pirates"):
            g.draw(src.owner, 2)

    return {"effects": (Effect(Timing.WHEN_ATTACKING, run, "Trash 1: reveal the top card; draw 2 if it's Rocks Pirates",
                               condition=lambda g, src: bool(g.players[src.owner].hand)),)}


# ---------------------------------------------------------------- replacement effects

def _by_opponent(type_check=lambda card: True):
    """Applies when one of your Characters would be removed from the field by your opponent's effect."""
    return lambda g, src, card, by, ko: (card.owner == src.owner and card.zone == "character"
                                         and by == 1 - src.owner and type_check(card))


def _trash_from_hand(n):
    def pay(g, src, card):
        yield from g.trash_from_hand(src.owner, n)
    return pay


def _hand_at_least(n):
    return lambda g, src: len(g.players[src.owner].hand) >= n


@script("OP17-015", covers="would be removed")
def marco():
    def pay(g, src, card):
        yield from g.ko(src, by=None, replaceable=False)
    return {"replacements": (Replacement(_by_opponent(), pay, lambda g, src: src.zone == "character",
                                         "K.O. Marco"),)}


@script("OP17-021", covers="would be removed")
def crone_oli():
    def pay(g, src, card):
        yield from g.rest_your_cards(src.owner, 1)
    return {"replacements": (Replacement(
        _by_opponent(lambda c: _types_include(c, "Red-Haired Pirates")), pay,
        lambda g, src: g.active_card_count(src.owner) >= 1, "Rest 1 of your cards"),)}


@script("OP17-023", covers="would be K.O.'d")
def nami():
    def applies(g, src, card, by, ko):
        return ko and card.owner == src.owner and card.zone == "character" and \
            bool({"East Blue", "Straw Hat Crew"} & set(card.d.types))

    def pay(g, src, card):
        src.rested = True
        yield from ()
    return {"replacements": (Replacement(applies, pay, lambda g, src: src.zone == "character" and not src.rested,
                                         "Rest Nami"),)}


@script("OP17-043", covers="would be removed")
def ganzui():
    return {"replacements": (Replacement(lambda g, src, card, by, ko: card is src, _trash_from_hand(2),
                                         _hand_at_least(2), "Trash 2 cards from your hand"),)}


@script("OP17-045", covers="would be removed")
def kyo():
    return {"replacements": (Replacement(_by_opponent(), _trash_from_hand(2), _hand_at_least(2),
                                         "Trash 2 cards from your hand"),)}


@script("OP17-095", covers="would be removed")
def zoro():
    def pay(g, src, card):
        trash = g.players[src.owner].trash
        for c in (yield from g.choose_cards(src.owner, trash, 3, "Place on the bottom of your deck", exactly=True)):
            g.move(c, "deck", bottom=True)
    return {"replacements": (Replacement(_by_opponent(), pay, lambda g, src: len(g.players[src.owner].trash) >= 3,
                                         "Place 3 cards from your trash at the bottom of your deck"),)}


# ---------------------------------------------------------------- other OP-17 cards

@script("OP17-007", covers="[On Play]")
def oden():
    """Plays a {Land of Wano} or "Whitebeard Pirates" Character with 6000 power or less (the power
    limit is read as covering both kinds)."""
    def run(g, src):
        leader = _leader(g, src)
        if leader.name != "Edward.Newgate" and "Land of Wano" not in leader.d.types:
            return
        cands = [c for c in g.players[src.owner].hand if c.category == Category.CHARACTER and c.d.power <= 6000
                 and ("Land of Wano" in c.d.types or _types_include(c, "Whitebeard Pirates"))]
        for c in (yield from g.choose_cards(src.owner, cands, 1, "Play a Character")):
            yield from g.play_card(c)
    return {"effects": (Effect(Timing.ON_PLAY, run, "Play a Wano or Whitebeard Character with 6000 power or less"),)}


@script("OP17-040", covers="attacks or is attacked")
def newgate_rocks():
    def cond(g, src):
        b, leader = g.battle, _leader(g, src)
        return (b is not None and leader in (b.attacker, b.target) and _types_include(leader, "Rocks Pirates")
                and bool(g.players[src.owner].hand))

    def run(g, src):
        if not (yield from g.confirm(src.owner, "Trash 1 card: your Leader gains +3000 power this battle?")):
            return
        yield from g.trash_from_hand(src.owner, 1)
        g.add_power(_leader(g, src), 3000, "battle", by=src.owner)
    return {"effects": (Effect(Timing.ATTACK_DECLARED, run, "Trash 1: your Leader gains +3000 power this battle",
                               once_per_turn=True, condition=cond),)}


@script("OP17-044", covers="cannot attack any card")
def captain_john():
    return {"attack_lock": lambda g, src: src.rested and _types_include(_leader(g, src), "Rocks Pirates")}


@script("OP17-062")
def kaido_don():
    def run(g, src):
        g.add_don(src.owner, 1)
        ps = g.players[src.owner]
        if ps.don_rested:
            ps.don_rested -= 1
            ps.don_active += 1
        yield from ()
    return {"effects": (Effect(Timing.DON_RETURNED, run, "Add 1 active DON!!, then set 1 DON!! as active",
                               your_turn=True, once_per_turn=True),)}


@script("OP17-063")
def kaido_negate():
    def counter(g, src, card):
        if card.owner == src.owner and card.zone == "hand" and card.category == Category.CHARACTER and not card.d.counter:
            return 1000
        return None

    def run(g, src):
        if src.played_turn != g.turn_number:
            return
        cands = [c for c in g.characters(1 - src.owner) if g.cost(c) <= 6]
        for c in (yield from g.choose_cards(src.owner, cands, 1, "Negate and K.O.")):
            g.negate(c, "turn", by=src.owner)
            yield from g.ko(c, by=src.owner)
    return {"static_counter": counter,
            "effects": (Effect(Timing.ACTIVATE_MAIN, run, "If played this turn: negate and K.O. a cost 6 or less",
                               once_per_turn=True, don_minus=1),)}


@script("OP17-112", covers="The base power")
def linlin_base_power():
    def base(g, src, card):
        if g.turn_player == src.owner and card.owner == src.owner and card.zone == "character" \
                and card.d.trigger_text and card.d.power == 4000:
            return 8000
        return None
    return {"static_base_power": base}


@script("OP17-118")
def xebec_character():
    def counter(g, card):
        return 2000 if all(not c.d.counter for c in g.characters(card.owner)) else None

    def run(g, src):
        g.draw(src.owner, 1)
        left, names = 9, set()
        for _ in range(2):
            hand = g.players[src.owner].hand
            cands = [c for c in hand if c.category in (Category.CHARACTER, Category.STAGE)
                     and "Rocks Pirates" in c.d.types and g.cost(c) <= left and c.name not in names]
            pick = yield from g.choose_cards(src.owner, cands, 1, f"Play a Rocks Pirates card (cost {left} or less)")
            if not pick:
                return
            left -= g.cost(pick[0])
            names.add(pick[0].name)
            yield from g.play_card(pick[0])
    return {"hand_counter": counter,
            "effects": (Effect(Timing.ON_PLAY, run, "Draw 1, then play up to 2 Rocks Pirates cards, total cost 9"),)}


@script("OP17-119", covers="+12 cost")
def loki():
    return {"static_cost": lambda g, src, card: 12 if card is src else 0,
            "static_power": lambda g, src, card: 3000 if card is src and g.turn_player != src.owner else 0}
