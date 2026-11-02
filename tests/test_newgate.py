"""OP17-040 Edward.Newgate: "[Once Per Turn] When your Leader with a type including "Rocks Pirates"
attacks or is attacked, you may trash 1 card from your hand to activate this effect. Your Leader
gains +3000 power during this battle." Run with: python3 -m unittest discover tests"""
import unittest

from optcg.cardpool import R_DECKHAND
from optcg.carddb import load_cards

from test_rules import battle_ready, opt

PROMPT = "your Leader gains +3000 power this battle"


class Newgate(unittest.TestCase):
    def setUp(self):
        self.cards = load_cards()
        self.g, self.att, self.me = battle_ready()  # self.att attacks on this turn; self.me has Newgate
        self.leader = self.put("OP17-039", self.me, "leader")  # Rocks.D.Xebec: type "Rocks Pirates"
        self.newgate = self.put("OP17-040", self.me)
        self.hand = [self.put(R_DECKHAND, self.me, "hand") for _ in range(3)]

    def put(self, card, player, zone="character"):
        c = self.g.create_card(self.cards[card] if isinstance(card, str) else card, player, zone)
        c.played_turn = -1
        self.g.refresh_options()
        return c

    def attack(self, attacker, target):
        """Declare the attack; return True if Newgate's question came up (left pending)."""
        g = self.g
        g.step(opt(g, "attack", attacker, target))
        for _ in range(10):
            d = g.pending
            if PROMPT in d.prompt:
                return True
            if d.kind in ("block", "counter", "main"):
                return False
            g.step(next((o for o in d.options if o.kind in ("no", "pass")), d.options[0]))
        raise AssertionError("the attack never reached a battle step")

    def finish_battle(self):
        g = self.g
        while g.pending.kind != "main" and not g.over:
            g.step(next((o for o in g.pending.options if o.kind in ("no", "pass")), g.pending.options[0]))

    def test_offered_when_your_leader_is_attacked(self):
        self.assertTrue(self.attack(self.g.players[self.att].leader, self.leader))

    def test_gives_3000_for_that_battle_only(self):
        g = self.g
        base = g.power(self.leader)
        self.attack(g.players[self.att].leader, self.leader)
        g.step(opt(g, "yes"))
        if g.pending.kind == "select":  # which card to trash
            g.step(g.pending.options[0])
        self.assertEqual(g.power(self.leader), base + 3000)
        self.assertEqual(len(g.players[self.me].hand), 2)
        self.finish_battle()
        self.assertEqual(g.power(self.leader), base)

    def test_once_per_turn(self):
        g = self.g
        self.put(R_DECKHAND, self.att)  # a second attacker
        second = g.players[self.att].characters[-1]
        self.attack(g.players[self.att].leader, self.leader)
        g.step(opt(g, "yes"))
        if g.pending.kind == "select":
            g.step(g.pending.options[0])
        self.finish_battle()
        g.refresh_options()
        self.assertFalse(self.attack(second, self.leader))

    def test_not_offered_with_an_empty_hand(self):
        g = self.g
        for c in list(g.players[self.me].hand):
            g.move(c, "trash")
        self.assertFalse(self.attack(g.players[self.att].leader, self.leader))

    def test_not_offered_when_a_character_is_attacked(self):
        g = self.g
        target = self.put(R_DECKHAND, self.me)
        target.rested = True
        g.refresh_options()
        self.assertFalse(self.attack(g.players[self.att].leader, target))

    def test_not_offered_without_a_rocks_pirates_leader(self):
        g = self.g
        g.create_card(self.cards["OP17-099"], self.me, "leader")  # Charlotte Linlin: not "Rocks Pirates"
        g.refresh_options()
        self.assertFalse(self.attack(g.players[self.att].leader, g.players[self.me].leader))

    def test_offered_when_your_leader_attacks(self):
        g = self.g
        while g.turn_player != self.me or g.pending.kind != "main":  # on to the Rocks player's turn
            g.step(next((o for o in g.pending.options if o.kind in ("end", "no", "pass")), g.pending.options[0]))
        self.leader.rested = False
        g.refresh_options()
        self.assertTrue(self.attack(self.leader, g.players[self.att].leader))


if __name__ == "__main__":
    unittest.main()
