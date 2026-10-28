"""Bot heuristics. Run with: python3 -m unittest discover tests"""
import unittest

from optcg import Game
from optcg.agents import GreedyAgent
from optcg.cardpool import GREEN_DECK, RED_DECK, R_DECKHAND
from optcg.play import play_game

from test_rules import add_life, new_game, to_turn, wipe


def attack_choice(g: Game):
    """Let greedy play its turn; return the first attack it declares, or None if it ends first."""
    bot = GreedyAgent()
    for _ in range(40):
        a = bot.act(g, g.pending)
        if g.pending.kind == "main" and a.kind in ("attack", "end"):
            return a if a.kind == "attack" else None
        g.step(a)  # a play, an activation, or a choice the effect asked for
    raise AssertionError("greedy never settled on an attack or the end of its turn")


class GreedyAttacks(unittest.TestCase):
    def setUp(self):
        self.g = new_game()
        to_turn(self.g, 3)
        wipe(self.g)
        for p in (0, 1):
            add_life(self.g, p, 3)
        self.g.players[0].leader.rested = True  # so only Characters can attack
        self.g.refresh_options()

    def character(self, player: int, rested: bool = False):
        c = self.g.create_card(R_DECKHAND, player, "character")  # 2000 power
        c.played_turn, c.rested = -1, rested
        self.g.refresh_options()
        return c

    def test_attacks_leader_with_a_weaker_character(self):
        """A 2000-power Character still swings at a 5000-power Leader: it forces a Counter."""
        small = self.character(0)
        a = attack_choice(self.g)
        self.assertIsNotNone(a, "greedy ended the turn instead of attacking")
        self.assertEqual((a.card, a.target), (small.uid, self.g.players[1].leader.uid))

    def test_prefers_a_winning_attack_over_chip_damage(self):
        self.character(0)
        weak = self.character(1, rested=True)  # a 2000-power Character the attacker can K.O.
        a = attack_choice(self.g)
        self.assertEqual(a.target, weak.uid)

    def test_ends_the_turn_with_nothing_to_attack_with(self):
        """Chip attacks don't make greedy invent attacks it has no cards for."""
        self.assertFalse(self.g.players[0].characters)  # and the Leader is rested
        self.assertIsNone(attack_choice(self.g))

    def test_a_deck_of_small_characters_still_races(self):
        """Without chip attacks, a side whose Characters never out-power the Leader never attacks."""
        attacks = sum(sum(" attacks " in line for line in
                          play_game((RED_DECK, GREEN_DECK), (GreedyAgent(), GreedyAgent()), seed=s).log)
                      for s in range(10))
        self.assertGreater(attacks / 10, 5)


if __name__ == "__main__":
    unittest.main()
