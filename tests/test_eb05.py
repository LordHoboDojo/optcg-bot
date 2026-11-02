"""EB-05 cards that needed new rules in the engine. Run with: python3 -m unittest discover tests"""
import unittest

from optcg import Action
from optcg.cardpool import R_DECKHAND
from optcg.cards import BLOCKER, NO_BLOCKER, NO_KO_BY_EFFECTS
from optcg.carddb import load_cards

from test_rules import battle_ready, opt

SHIRAHOSHI = "OP11-022"  # the Leader [Shirahoshi]


def drive(gen, *answers):
    """Run an engine generator, answering its decisions in order; returns its result."""
    answers = list(answers)
    try:
        d = next(gen)
        while True:
            a = answers.pop(0)
            d = gen.send(next(o for o in d.options if o.kind == a) if isinstance(a, str) else a)
    except StopIteration as e:
        return e.value


class EB05(unittest.TestCase):
    def setUp(self):
        self.cards = load_cards()
        self.g, p, _ = battle_ready()
        assert p == 0  # player 0 attacks in these tests

    def put(self, card_id, player, zone="character"):
        c = self.g.create_card(self.cards[card_id] if isinstance(card_id, str) else card_id, player, zone)
        c.played_turn = -1
        self.g.refresh_options()
        return c

    # ------------------------------------------------ Gloriosa: "if you would take damage ... instead"

    def test_gloriosa_takes_the_damage_instead(self):
        g = self.g
        glo = self.put("EB05-052", 1)
        life = len(g.players[1].life)
        g.step(opt(g, "attack", g.players[0].leader, g.players[1].leader))
        while g.pending.kind != "confirm":
            g.step(opt(g, "pass"))
        self.assertIn("Gloriosa", g.pending.prompt)
        g.step(opt(g, "yes"))
        self.assertEqual(glo.zone, "trash")
        self.assertEqual(len(g.players[1].life), life)

    def test_gloriosa_can_be_declined(self):
        g = self.g
        glo = self.put("EB05-052", 1)
        life = len(g.players[1].life)
        g.step(opt(g, "attack", g.players[0].leader, g.players[1].leader))
        while g.pending.kind != "confirm":
            g.step(opt(g, "pass"))
        g.step(opt(g, "no"))
        self.assertEqual(glo.zone, "character")
        self.assertEqual(len(g.players[1].life), life - 1)

    # ------------------------------------------------ Nico Robin Leader: when a <Wisdom> Character is K.O.'d

    def test_robin_leader_adds_life_when_a_wisdom_character_is_kod(self):
        g = self.g
        self.put("EB05-010", 0, "leader")
        otohime = self.put("EB05-011", 0)  # <Wisdom>, no [Blocker]
        life = len(g.players[0].life)
        drive(g.ko(otohime, by=1))
        drive(g.resolve_triggers(), "yes")  # "up to 1": add the top card
        self.assertEqual(len(g.players[0].life), life + 1)
        # [Once Per Turn]: a second K.O. this turn adds nothing
        drive(g.ko(self.put("EB05-011", 0), by=1))
        self.assertFalse(g._queue)

    def test_robin_leader_ignores_blockers_and_other_attributes(self):
        g = self.g
        self.put("EB05-010", 0, "leader")
        blocker = self.put("EB05-011", 0)
        g.grant_keyword(blocker, BLOCKER)
        drive(g.ko(blocker, by=1))
        self.assertFalse(g._queue)
        drive(g.ko(self.put(R_DECKHAND, 0), by=1))  # no <Wisdom>
        self.assertFalse(g._queue)

    # ------------------------------------------------ Nami EB05-061: once-per-turn replacement

    def test_nami_saves_a_character_from_the_opponent_once_per_turn(self):
        g = self.g
        self.put("EB05-061", 0)
        a, b = self.put(R_DECKHAND, 0), self.put(R_DECKHAND, 0)
        life, hand = len(g.players[0].life), len(g.players[0].hand)
        self.assertFalse(drive(g.remove_from_field(a, "hand", by=1), "yes"))
        self.assertEqual(a.zone, "character")
        self.assertEqual((len(g.players[0].life), len(g.players[0].hand)), (life - 1, hand + 1))
        self.assertTrue(drive(g.remove_from_field(b, "hand", by=1)))  # used this turn: no offer

    def test_nami_covers_battle_but_not_your_own_effects(self):
        g = self.g
        self.put("EB05-061", 0)
        a = self.put(R_DECKHAND, 0)
        self.assertTrue(drive(g.remove_from_field(a, "hand", by=0)))  # your own effect: no offer
        b = self.put(R_DECKHAND, 0)
        gen = g.ko(b, by=None, in_battle=True)
        self.assertIn("Nami", next(gen).prompt)

    # ------------------------------------------------ costs and markers

    def test_a_prevented_ko_cost_stops_the_effect(self):
        """8-3-1-7: Special Hollow's K.O. cost isn't paid if the K.O. doesn't happen."""
        g = self.g
        perona = self.put("EB05-043", 0)
        g.grant_keyword(perona, NO_KO_BY_EFFECTS)
        target = self.put(R_DECKHAND, 1)
        hollow = self.put("EB05-049", 0, "hand")
        g.step(opt(g, "play", hollow))
        while g.pending.kind == "confirm":
            g.step(opt(g, "yes"))
        self.assertEqual(perona.zone, "character")
        self.assertEqual(g.cost(target), target.d.cost)  # no -3 cost

    def test_cannot_activate_blocker(self):
        g = self.g
        blocker = self.put(R_DECKHAND, 1)
        g.grant_keyword(blocker, BLOCKER)
        g.grant_keyword(blocker, NO_BLOCKER)
        g.step(opt(g, "attack", g.players[0].leader, g.players[1].leader))
        self.assertNotEqual(g.pending.kind, "block")

    def test_returning_active_don_sets_off_kaido(self):
        """You Need Me's cost returns DON!! to the DON!! deck, which OP17-062 Kaido watches for."""
        g = self.g
        self.put("OP17-062", 0)
        event = self.put("EB05-040", 0, "hand")
        ps = g.players[0]
        g.step(opt(g, "play", event))
        while g.pending.kind == "confirm":
            g.step(opt(g, "yes"))
        self.assertTrue(any("Kaido" in line for line in g.log[-4:]), g.log[-4:])

    def test_resting_your_named_leader_as_a_cost(self):
        """EB05-020: playable only with an active Leader [Shirahoshi], which it rests."""
        g = self.g
        event = self.put("EB05-020", 0, "hand")
        self.assertFalse(any(o.card == event.uid for o in g.pending.options))  # wrong Leader
        leader = self.put(SHIRAHOSHI, 0, "leader")
        hand = len(g.players[0].hand)
        g.step(opt(g, "play", event))
        while g.pending.kind == "confirm":
            g.step(opt(g, "yes"))
        self.assertTrue(leader.rested)
        self.assertEqual(len(g.players[0].hand), hand - 1 + 2)


if __name__ == "__main__":
    unittest.main()
