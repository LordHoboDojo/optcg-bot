"""A small made-up card pool that exercises the engine's mechanics.

These are not real cards. A real card database should be added later; each
card's effects are written the same way as below.
"""
from __future__ import annotations

from .cards import (BANISH, BLOCKER, DOUBLE_ATTACK, RUSH, UNBLOCKABLE, CardDef, Category,
                    Effect, Timing)
from .deck import Deck

L, C, E, S = Category.LEADER, Category.CHARACTER, Category.EVENT, Category.STAGE


# ---------------------------------------------------------------- effect code

def _up_to_one(g, player, candidates, prompt):
    return (yield from g.choose_cards(player, candidates, 1, prompt))


def red_leader_rush(g, src):
    for c in (yield from _up_to_one(g, src.owner, g.characters(src.owner), "Give [Rush] this turn")):
        g.grant_keyword(c, RUSH, "turn", by=src.owner)


def green_leader_untap_don(g, src):
    ps = g.players[src.owner]
    if ps.don_rested:
        ps.don_rested -= 1
        ps.don_active += 1


def minus_2000(g, src):
    opp = g.characters(g.opponent(src.owner))
    for c in (yield from _up_to_one(g, src.owner, opp, "Give -2000 power this turn")):
        g.add_power(c, -2000, "turn", by=src.owner)


def self_plus_2000_battle(g, src):
    g.add_power(src, 2000, "battle", by=src.owner)


def play_self(g, src):
    yield from g.play_card(src)


def plus_4000_battle(g, src):
    for c in (yield from _up_to_one(g, src.owner, g.leader_and_characters(src.owner), "+4000 power this battle")):
        g.add_power(c, 4000, "battle", by=src.owner)


def plus_2000_battle(g, src):
    for c in (yield from _up_to_one(g, src.owner, g.leader_and_characters(src.owner), "+2000 power this battle")):
        g.add_power(c, 2000, "battle", by=src.owner)


def plus_1000_turn(g, src):
    for c in (yield from _up_to_one(g, src.owner, g.leader_and_characters(src.owner), "+1000 power this turn")):
        g.add_power(c, 1000, "turn", by=src.owner)


def char_plus_1000_turn(g, src):
    for c in (yield from _up_to_one(g, src.owner, g.characters(src.owner), "+1000 power this turn")):
        g.add_power(c, 1000, "turn", by=src.owner)


def ko_power_5000_or_less(g, src):
    opp = [c for c in g.characters(g.opponent(src.owner)) if g.power(c) <= 5000]
    for c in (yield from _up_to_one(g, src.owner, opp, "K.O. a Character with 5000 power or less")):
        yield from g.ko(c, by=src.owner)


def rest_cost_4_or_less(g, src):
    opp = [c for c in g.characters(g.opponent(src.owner)) if not c.rested and g.cost(c) <= 4]
    for c in (yield from _up_to_one(g, src.owner, opp, "Rest a Character with cost 4 or less")):
        g.rest(c)


def draw_1_trash_1(g, src):
    g.draw(src.owner, 1)
    yield from g.trash_from_hand(src.owner, 1)


def draw_2_trash_1(g, src):
    g.draw(src.owner, 2)
    yield from g.trash_from_hand(src.owner, 1)


def draw_2(g, src):
    g.draw(src.owner, 2)


def ramp_rested_don(g, src):
    g.add_don(src.owner, 1, rested=True)


# ---------------------------------------------------------------- red

RED_LEADER = CardDef("SMP-L01", "Red Captain", L, ("red",), power=5000, life=5, effects=(
    Effect(Timing.ACTIVATE_MAIN, red_leader_rush, "(1): Up to 1 of your Characters gains [Rush] this turn.",
           once_per_turn=True, rest_don=1),))

R_DECKHAND = CardDef("SMP-R01", "Deckhand", C, ("red",), cost=1, power=2000, counter=1000)
R_SWORDSMAN = CardDef("SMP-R02", "Swordsman Recruit", C, ("red",), cost=2, power=3000, counter=1000, effects=(
    Effect(Timing.ON_PLAY, minus_2000, "Give up to 1 of your opponent's Characters -2000 power this turn."),))
R_LOOKOUT = CardDef("SMP-R03", "Lookout", C, ("red",), cost=2, power=2000, counter=1000,
                    keywords=frozenset({BLOCKER}))
R_BRAWLER = CardDef("SMP-R04", "Brawler", C, ("red",), cost=3, power=4000, counter=1000,
                    keywords=frozenset({RUSH}))
R_FIRST_MATE = CardDef("SMP-R05", "First Mate", C, ("red",), cost=4, power=5000, counter=1000, effects=(
    Effect(Timing.WHEN_ATTACKING, self_plus_2000_battle, "This Character gains +2000 power during this battle.",
           don_req=1),))
R_FIRE_FIST = CardDef("SMP-R06", "Fire Fist", C, ("red",), cost=5, power=6000,
                      keywords=frozenset({DOUBLE_ATTACK}))
R_ROOKIE = CardDef("SMP-R07", "Rookie Gunner", C, ("red",), cost=1, power=1000, counter=2000, effects=(
    Effect(Timing.TRIGGER, play_self, "Play this card."),))
R_BRUISER = CardDef("SMP-R08", "Harbor Bruiser", C, ("red",), cost=3, power=5000)
R_VETERAN = CardDef("SMP-R09", "Grizzled Veteran", C, ("red",), cost=4, power=6000)
R_ADMIRAL = CardDef("SMP-R10", "Iron Admiral", C, ("red",), cost=7, power=9000)
R_GUARD = CardDef("SMP-R11", "Guard Point", E, ("red",), cost=1, effects=(
    Effect(Timing.COUNTER, plus_4000_battle, "Up to 1 of your Leader or Characters gains +4000 power this battle."),
    Effect(Timing.TRIGGER, plus_1000_turn, "Up to 1 of your Leader or Characters gains +1000 power this turn.")))
R_FLAME = CardDef("SMP-R12", "Flame Strike", E, ("red",), cost=3, effects=(
    Effect(Timing.MAIN, ko_power_5000_or_less, "K.O. up to 1 of your opponent's Characters with 5000 power or less."),
    Effect(Timing.TRIGGER, ko_power_5000_or_less, "Activate this card's [Main] effect.")))
R_SHIP = CardDef("SMP-R13", "Pirate Ship", S, ("red",), cost=1, effects=(
    Effect(Timing.ACTIVATE_MAIN, char_plus_1000_turn, "Rest this Stage: Up to 1 of your Characters gains +1000 power this turn.",
           rest_self=True),))

RED_DECK = Deck.from_counts(RED_LEADER, {
    R_DECKHAND: 4, R_SWORDSMAN: 4, R_LOOKOUT: 4, R_BRAWLER: 4, R_FIRST_MATE: 4, R_FIRE_FIST: 4,
    R_ROOKIE: 4, R_BRUISER: 4, R_VETERAN: 4, R_ADMIRAL: 2, R_GUARD: 4, R_FLAME: 4, R_SHIP: 4,
})

# ---------------------------------------------------------------- green

GREEN_LEADER = CardDef("SMP-L02", "Green Navigator", L, ("green",), power=5000, life=5, effects=(
    Effect(Timing.END_OF_YOUR_TURN, green_leader_untap_don, "Set up to 1 of your DON!! cards as active."),))

G_SCOUT = CardDef("SMP-G01", "Vine Scout", C, ("green",), cost=1, power=2000, counter=1000)
G_GUARD = CardDef("SMP-G02", "Forest Guard", C, ("green",), cost=2, power=3000,
                  keywords=frozenset({BLOCKER}))
G_HERBALIST = CardDef("SMP-G03", "Herbalist", C, ("green",), cost=2, power=2000, counter=2000, effects=(
    Effect(Timing.ON_PLAY, rest_cost_4_or_less, "Rest up to 1 of your opponent's Characters with a cost of 4 or less."),))
G_PLANTER = CardDef("SMP-G04", "Seed Planter", C, ("green",), cost=3, power=4000, counter=1000, effects=(
    Effect(Timing.ACTIVATE_MAIN, draw_1_trash_1, "Rest this Character: Draw 1 card, then trash 1 card from your hand.",
           rest_self=True, optional=False),))
G_THORN = CardDef("SMP-G05", "Thorn Knight", C, ("green",), cost=4, power=5000, counter=1000,
                  keywords=frozenset({BANISH}))
G_TREANT = CardDef("SMP-G06", "Ancient Treant", C, ("green",), cost=6, power=8000, effects=(
    Effect(Timing.ON_KO, draw_2, "Draw 2 cards."),))
G_CHARGER = CardDef("SMP-G07", "Wild Charger", C, ("green",), cost=5, power=6000,
                    keywords=frozenset({UNBLOCKABLE}))
G_SAPLING = CardDef("SMP-G08", "Sapling", C, ("green",), cost=1, power=1000, counter=2000, effects=(
    Effect(Timing.TRIGGER, play_self, "Play this card."),))
G_BOAR = CardDef("SMP-G09", "Tusked Boar", C, ("green",), cost=3, power=5000)
G_BEAR = CardDef("SMP-G10", "Moss Bear", C, ("green",), cost=4, power=6000)
G_ENTANGLE = CardDef("SMP-G11", "Entangle", E, ("green",), cost=1, effects=(
    Effect(Timing.COUNTER, plus_2000_battle, "Up to 1 of your Leader or Characters gains +2000 power this battle."),
    Effect(Timing.TRIGGER, rest_cost_4_or_less, "Rest up to 1 of your opponent's Characters with a cost of 4 or less.")))
G_SPROUT = CardDef("SMP-G12", "Sprout", E, ("green",), cost=2, effects=(
    Effect(Timing.MAIN, draw_2_trash_1, "Draw 2 cards, then trash 1 card from your hand."),
    Effect(Timing.TRIGGER, draw_2_trash_1, "Draw 2 cards, then trash 1 card from your hand.")))
G_GROVE = CardDef("SMP-G13", "Sacred Grove", S, ("green",), cost=2, effects=(
    Effect(Timing.ACTIVATE_MAIN, ramp_rested_don, "[Once Per Turn] Rest this Stage: Add up to 1 DON!! card from your DON!! deck and rest it.",
           rest_self=True, once_per_turn=True),))

GREEN_DECK = Deck.from_counts(GREEN_LEADER, {
    G_SCOUT: 4, G_GUARD: 4, G_HERBALIST: 4, G_PLANTER: 4, G_THORN: 4, G_TREANT: 4, G_CHARGER: 4,
    G_SAPLING: 4, G_BOAR: 4, G_BEAR: 4, G_ENTANGLE: 4, G_SPROUT: 4, G_GROVE: 2,
})

ALL_CARDS = {d.id: d for d in [
    RED_LEADER, R_DECKHAND, R_SWORDSMAN, R_LOOKOUT, R_BRAWLER, R_FIRST_MATE, R_FIRE_FIST, R_ROOKIE,
    R_BRUISER, R_VETERAN, R_ADMIRAL, R_GUARD, R_FLAME, R_SHIP,
    GREEN_LEADER, G_SCOUT, G_GUARD, G_HERBALIST, G_PLANTER, G_THORN, G_TREANT, G_CHARGER, G_SAPLING,
    G_BOAR, G_BEAR, G_ENTANGLE, G_SPROUT, G_GROVE,
]}
