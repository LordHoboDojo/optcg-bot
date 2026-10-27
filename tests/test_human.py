"""The terminal play mode, driven with scripted input. Run with: python3 -m unittest discover tests"""
import random
import unittest

from optcg.agents import GreedyAgent
from optcg.cardpool import GREEN_DECK, RED_DECK
from optcg.human import HumanAgent, render_board
from optcg.play import play_human


def scripted(answers):
    it = iter(answers)
    return lambda prompt: next(it)


class HumanMode(unittest.TestCase):
    def test_full_game_with_bad_input_and_commands(self):
        rng = random.Random(0)
        out = []

        def typing(prompt):
            r = rng.random()
            return "junk" if r < 0.05 else "t 2" if r < 0.08 else "b" if r < 0.1 else str(rng.randint(1, 3))

        g = play_human((RED_DECK, GREEN_DECK), 0, GreedyAgent(), seed=1, human=HumanAgent(typing, out.append))
        self.assertTrue(g.over)
        text = "\n".join(out)
        self.assertIn("Type an option number", text)  # shown again after bad input
        self.assertIn("== YOU ==", text)
        self.assertRegex(text, r"You (win|lose)!?|Draw")

    def test_concede(self):
        out = []
        g = play_human((RED_DECK, GREEN_DECK), 1, GreedyAgent(), seed=2,
                       human=HumanAgent(scripted(["q"] * 10), out.append))
        self.assertEqual((g.over, g.winner), (True, 0))

    def test_board_hides_opponents_hand(self):
        g = play_human((RED_DECK, GREEN_DECK), 0, GreedyAgent(), seed=3,
                       human=HumanAgent(scripted(["q"]), lambda s: None))
        board = render_board(g, 0)
        opp_hand = [c.name for c in g.players[1].hand]
        mine = board.split("== YOU ==")[1]
        theirs = board.split("== YOU ==")[0]
        self.assertIn("Your hand:", mine)
        self.assertNotIn("Your hand:", theirs)
        self.assertTrue(all(name not in theirs.split("Characters")[0] for name in opp_hand))


if __name__ == "__main__":
    unittest.main()
