"""The training environment. Run with: python3 -m unittest discover tests"""
import random
import unittest

from optcg.agents import GreedyAgent, RandomAgent
from optcg.cardpool import GREEN_DECK, RED_DECK
from optcg.env import CARD_FIELDS, OPTION_FIELDS, SCALAR_FIELDS, Env, card_index


def play_random(env, seed):
    rng = random.Random(seed)
    obs, steps, reward = env.reset(seed), 0, 0.0
    while not env.done:
        assert env.decision.player == env.seat
        obs, reward, done = env.step(rng.randrange(len(obs["options"])))
        steps += 1
        if obs:
            yield obs
    yield reward


class EnvShape(unittest.TestCase):
    def test_rows_have_fixed_widths(self):
        env = Env(RED_DECK, GREEN_DECK, GreedyAgent(), seat=1)
        for obs in play_random(env, 3):
            if isinstance(obs, float):
                break
            self.assertEqual(len(obs["scalars"]), len(SCALAR_FIELDS))
            self.assertTrue(all(len(r) == len(CARD_FIELDS) for r in obs["cards"]))
            self.assertTrue(all(len(r) == len(OPTION_FIELDS) for r in obs["options"]))
            self.assertEqual(obs["player"], 1)
            for o in obs["options"]:  # option rows point at card rows
                for i in o[1:3]:
                    self.assertTrue(i == -1 or 0 <= i < len(obs["cards"]))

    def test_reward_matches_winner(self):
        for seed in range(5):
            env = Env(RED_DECK, GREEN_DECK, RandomAgent(seed))
            reward = list(play_random(env, seed))[-1]
            self.assertTrue(env.game.over)
            expected = 0.0 if env.game.winner is None else 1.0 if env.game.winner == 0 else -1.0
            self.assertEqual(reward, expected)

    def test_same_seed_same_game(self):
        logs = []
        for _ in range(2):
            env = Env(RED_DECK, GREEN_DECK, RandomAgent(7))
            list(play_random(env, 7))
            logs.append(env.game.log)
        self.assertEqual(logs[0], logs[1])

    def test_truncation(self):
        env = Env(RED_DECK, GREEN_DECK, RandomAgent(0), max_steps=10)
        reward = list(play_random(env, 0))[-1]
        self.assertTrue(env.truncated)
        self.assertEqual(reward, 0.0)


class EnvHiddenInfo(unittest.TestCase):
    def test_opponent_hand_and_deck_not_encoded(self):
        env = Env(RED_DECK, GREEN_DECK, GreedyAgent())
        obs = env.reset(2)
        g = env.game
        ids = card_index()
        mine = [r for r in obs["cards"] if r[1] == 1.0]
        theirs = [r for r in obs["cards"] if r[1] == 0.0]
        self.assertEqual(len([r for r in mine if r[2] == 0]), len(g.players[0].hand))  # zone 0 = hand
        self.assertFalse([r for r in theirs if r[2] in (0, 6)])  # no opponent hand or deck rows
        self.assertEqual(theirs[0][0], ids[g.players[1].leader.d.id])


if __name__ == "__main__":
    unittest.main()
