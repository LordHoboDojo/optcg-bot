"""Finer rules: effect order (8-6), timing windows, counters, costs, protections, edge cases.
Run with: python3 -m unittest discover tests"""
import unittest

from optcg import Action, CardDef, Category, Effect, Timing
from optcg.cardpool import R_BRUISER, R_DECKHAND, R_LOOKOUT
from optcg.cards import BLOCKER, RUSH_CHARACTER, UNBLOCKABLE
from optcg.effect_text import compile_card

from test_rules import add_life, battle_ready, has_opt, new_game, opt, to_turn

C = Category.CHARACTER


def recorder(log, name):
    def run(g, src):
        log.append(name)
        yield from ()
    return run


def char(name, power=3000, cost=1, effects=(), keywords=(), **kw):
    return CardDef(f"T-{name}", name, C, ("red",), cost=cost, power=power, effects=tuple(effects),
                   keywords=frozenset(keywords), **kw)


def from_text(name, text, power=3000, cost=1):
    comp = compile_card(text)
    assert not comp.unimplemented, comp.unimplemented
    return char(name, power, cost, comp.effects, comp.keywords, static_keywords=comp.static_keywords_fn(),
                text=text)


def attack(g, attacker, target):
    g.step(opt(g, "attack", card=attacker, target=target))


def answer_all(g, player, kind="pass"):
    """Answer `player`'s decisions with `kind` (or the first option) until it's someone else's turn to act."""
    while not g.over and g.pending.player == player and g.pending.kind != "main":
        g.step(opt(g, kind) if has_opt(g, kind) else g.pending.options[0])


class EffectOrder(unittest.TestCase):
    def test_triggered_effect_waits_for_the_effect_that_caused_it(self):
        """8-6-3: an [On Play] caused by an effect resolves after that effect finishes."""
        g, p, o = battle_ready()
        log = []
        y_def = char("Y", effects=[Effect(Timing.ON_PLAY, recorder(log, "Y on play"))])

        def play_y_then_note(g, src):
            y = next(c for c in g.players[src.owner].hand if c.name == "Y")
            yield from g.play_card(y)
            log.append("X finished")

        x = g.create_card(char("X", effects=[Effect(Timing.ACTIVATE_MAIN, play_y_then_note)]), p, "character")
        g.create_card(y_def, p, "hand")
        g.refresh_options()
        g.step(opt(g, "activate", card=x))
        self.assertEqual(log, ["X finished", "Y on play"])

    def test_turn_order_batches_and_player_chosen_order(self):
        """8-6-1: turn player first, choosing the order; 8-6-1-1: a new trigger waits for the batch."""
        g, p, o = battle_ready()
        log = []
        c_def = char("C", effects=[Effect(Timing.ON_PLAY, recorder(log, "C"))])

        def a_effect(g, src):
            log.append("A")
            c = next(c for c in g.players[src.owner].hand if c.name == "C")
            yield from g.play_card(c)

        g.create_card(char("A", effects=[Effect(Timing.ATTACK_DECLARED, a_effect)]), p, "character")
        g.create_card(char("B", effects=[Effect(Timing.ATTACK_DECLARED, recorder(log, "B"))]), p, "character")
        g.create_card(char("X", effects=[Effect(Timing.ATTACK_DECLARED, recorder(log, "X"))]), o, "character")
        g.create_card(c_def, p, "hand")
        g.refresh_options()
        attack(g, g.players[p].leader, g.players[o].leader)
        self.assertEqual((g.pending.kind, g.pending.player), ("order", p))
        g.step(next(a for a in g.pending.options if "A:" in a.label or a.label.startswith("A")))
        answer_all(g, o)
        self.assertEqual(log, ["A", "B", "X", "C"])

    def test_effects_triggered_during_damage_wait(self):
        """8-6-2: an [On Play] from a [Trigger] resolves only after all damage is dealt."""
        g, p, o = battle_ready()
        log = []

        def note_life(g, src):
            log.append(f"on play with {len(g.players[o].life)} life")
            yield from ()

        def play_self(g, src):
            yield from g.play_card(src)

        trig = char("Trig", effects=[Effect(Timing.ON_PLAY, note_life), Effect(Timing.TRIGGER, play_self)])
        g.create_card(trig, o, "life")  # top of Life: the first damage
        ff = g.create_card(char("Hitter", power=9000, keywords=["Double Attack"]), p, "character")
        ff.played_turn = 1
        g.refresh_options()
        attack(g, ff, g.players[o].leader)
        g.step(opt(g, "yes"))  # activate the [Trigger]
        answer_all(g, o)
        self.assertEqual(log, [f"on play with {5 - 1} life"])  # after the second damage too

    def test_on_ko_resolves_from_trash(self):
        g, p, o = battle_ready()
        seen = []

        def where(g, src):
            seen.append(src.zone)
            yield from ()

        victim = g.create_card(char("V", effects=[Effect(Timing.ON_KO, where)]), o, "character")
        victim.rested = True
        g.refresh_options()
        attack(g, g.players[p].leader, victim)
        answer_all(g, o)
        self.assertEqual(seen, ["trash"])


class TimingWindows(unittest.TestCase):
    def test_start_of_turn_main_and_end_of_battle(self):
        g, p, o = battle_ready()
        log = []
        g.create_card(char("S", effects=[Effect(Timing.START_OF_TURN, recorder(log, "turn")),
                                         Effect(Timing.START_OF_MAIN, recorder(log, "main")),
                                         Effect(Timing.END_OF_BATTLE, recorder(log, "battle"))]), p, "character")
        g.refresh_options()
        attack(g, g.players[p].leader, g.players[o].leader)
        answer_all(g, o)
        self.assertEqual(log, ["battle"])
        to_turn(g, 4)
        self.assertEqual(log, ["battle", "turn", "main"])  # the opponent's turn counts too

    def test_end_of_opponents_turn(self):
        g, p, o = battle_ready()
        log = []
        g.create_card(char("E", effects=[Effect(Timing.END_OF_OPP_TURN, recorder(log, "eoot"))]), o, "character")
        to_turn(g, 4)
        self.assertEqual(log, ["eoot"])

    def test_on_block(self):
        g, p, o = battle_ready()
        log = []
        blocker = g.create_card(char("Bl", power=1000, keywords=[BLOCKER],
                                     effects=[Effect(Timing.ON_BLOCK, recorder(log, "block"))]), o, "character")
        g.refresh_options()
        attack(g, g.players[p].leader, g.players[o].leader)
        g.step(opt(g, "block", card=blocker))
        self.assertEqual(log, ["block"])


class BattleDetails(unittest.TestCase):
    def test_counter_can_boost_any_of_your_cards(self):
        g, p, o = battle_ready()
        other = g.create_card(R_BRUISER, o, "character")
        counter = g.create_card(R_DECKHAND, o, "hand")
        attack(g, g.players[p].leader, g.players[o].leader)
        self.assertTrue(has_opt(g, "counter", card=counter, target=other))
        g.step(opt(g, "counter", card=counter, target=other))
        answer_all(g, o)
        self.assertEqual(len(g.players[o].life), 4)  # the Leader wasn't boosted

    def test_unblockable(self):
        g, p, o = battle_ready()
        g.create_card(R_LOOKOUT, o, "character")
        g.grant_keyword(g.players[p].leader, UNBLOCKABLE)
        g.refresh_options()
        attack(g, g.players[p].leader, g.players[o].leader)
        self.assertNotEqual(getattr(g.pending, "kind", None), "block")

    def test_battle_stops_if_attacker_leaves(self):
        """7-1-1-4: if the attacker moves during the Attack Step, skip to the End of the Battle."""
        g, p, o = battle_ready()

        def bounce_self(g, src):
            g.move(src, "hand")
            yield from ()

        atk = g.create_card(char("Flee", power=9000, effects=[Effect(Timing.WHEN_ATTACKING, bounce_self)]),
                            p, "character")
        atk.played_turn = 1
        g.refresh_options()
        attack(g, atk, g.players[o].leader)
        self.assertEqual(len(g.players[o].life), 5)
        self.assertIsNone(g.battle)

    def test_rush_character_only_attacks_characters(self):
        g, p, o = battle_ready()
        rested = g.create_card(R_BRUISER, o, "character")
        rested.rested = True
        rc = g.create_card(char("RC", keywords=[RUSH_CHARACTER]), p, "hand")
        g.players[p].don_active = 5
        g.refresh_options()
        g.step(opt(g, "play", card=rc))
        targets = {a.target for a in g.pending.options if a.kind == "attack" and a.card == rc.uid}
        self.assertEqual(targets, {rested.uid})


class CostsAndProtections(unittest.TestCase):
    def test_player_chooses_which_don_to_return(self):
        g, p, o = battle_ready()
        src = g.create_card(char("Ret", effects=[Effect(Timing.ACTIVATE_MAIN, recorder([], "x"), don_minus=1)]),
                            p, "character")
        g.refresh_options()
        g.step(opt(g, "give_don", target=src))
        g.step(opt(g, "activate", card=src))
        self.assertEqual(g.pending.kind, "return_don")
        g.step(opt(g, "select", card=src))
        self.assertEqual((src.don, g.players[p].don_deck), (0, 8))  # 10 - 3 drawn so far + 1 returned

    def test_owner_orders_cards_put_on_the_bottom(self):
        g, p, o = battle_ready()
        cards = [g.create_card(char(n), p, "hand") for n in "ABC"]

        def bottom_three(g, src):
            yield from g.place_in_order(p, cards, "deck", bottom=True)

        mover = g.create_card(char("M", effects=[Effect(Timing.ACTIVATE_MAIN, bottom_three)]), p, "character")
        g.refresh_options()
        g.step(opt(g, "activate", card=mover))
        g.step(opt(g, "select", card=cards[2]))  # C on top of the group
        g.step(opt(g, "select", card=cards[0]))  # then A, leaving B lowest
        self.assertEqual([c.name for c in g.players[p].deck[:3]], ["B", "A", "C"])

    def test_cannot_be_ko_in_battle(self):
        g, p, o = battle_ready()
        tough = g.create_card(from_text("Tough", "This Character cannot be K.O.'d in battle.", power=1000), o,
                              "character")
        tough.rested = True
        g.refresh_options()
        attack(g, g.players[p].leader, tough)
        answer_all(g, o)
        self.assertIn(tough, g.players[o].characters)
        from optcg.engine import Game  # an effect K.O. still works
        from test_carddb import run_effect
        run_effect(g.ko(tough, by=p))
        self.assertIn(tough, g.players[o].trash)

    def test_cannot_be_ko_by_opponents_effects(self):
        from test_carddb import run_effect
        g, p, o = battle_ready()
        text = "This Character cannot be K.O.'d by your opponent's effects."
        a = g.create_card(from_text("Safe", text), o, "character")
        run_effect(g.ko(a, by=p))
        self.assertIn(a, g.players[o].characters)
        run_effect(g.ko(a, by=o))
        self.assertIn(a, g.players[o].trash)

    def test_face_up_life_cost(self):
        g, p, o = battle_ready()
        flip = g.create_card(from_text("Flip", "[Activate: Main] You may turn 1 card from the top of your Life "
                                               "cards face-up: Draw 1 card."), p, "character")
        g.refresh_options()
        g.step(opt(g, "activate", card=flip))
        self.assertTrue(g.players[p].life[-1].face_up)
        self.assertEqual(g.observe(o)["opponent"]["life_face_up"], {4: g.players[p].life[-1].d.id})
        self.assertEqual(len(g.players[p].hand), 1)
        self.assertFalse(has_opt(g, "activate", card=flip))  # the top card is already face-up


class GameFlow(unittest.TestCase):
    def test_mulligan_redraws_five(self):
        from optcg.cardpool import GREEN_DECK, RED_DECK
        from optcg.engine import Game
        g = Game((RED_DECK, GREEN_DECK), seed=3)
        g.step(Action("first"))
        first = g.pending.player
        before = list(g.players[first].hand)
        g.step(Action("mulligan"))
        self.assertEqual(len(g.players[first].hand), 5)
        self.assertNotEqual(before, g.players[first].hand)

    def test_don_deck_running_out(self):
        g, p, o = battle_ready()
        g.players[o].don_deck = 1
        to_turn(g, 4)
        self.assertEqual(g.players[o].don_deck, 0)
        to_turn(g, 6)
        self.assertEqual(g.players[o].don_deck, 0)

    def test_both_decks_empty_is_a_draw(self):
        g, p, o = battle_ready()
        for ps in g.players:
            for c in list(ps.deck):
                g.move(c, "trash")
        g.step(opt(g, "end"))
        self.assertTrue(g.over)
        self.assertIsNone(g.winner)

    def test_concede(self):
        g, p, o = battle_ready()
        g.concede(p)
        self.assertEqual((g.over, g.winner), (True, o))

    def test_leader_never_leaves(self):
        g, p, o = battle_ready()
        leader = g.players[p].leader
        g.move(leader, "trash")
        self.assertIs(g.players[p].leader, leader)
        self.assertEqual(leader.zone, "leader")


if __name__ == "__main__":
    unittest.main()
