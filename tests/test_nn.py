"""Neural bots. Skipped without PyTorch; run with: .venv/bin/python -m unittest discover tests"""
import tempfile
import unittest
from pathlib import Path

try:
    import torch
except ImportError:
    torch = None

from optcg.agents import GreedyAgent
from optcg.cardpool import GREEN_DECK, RED_DECK
from optcg.env import Env
from optcg.play import play_game


@unittest.skipIf(torch is None, "PyTorch not installed")
class NeuralBot(unittest.TestCase):
    def setUp(self):
        from optcg.nn.model import PolicyNet
        torch.manual_seed(0)
        self.net = PolicyNet(hidden=32)

    def observations(self, n=20):
        env = Env(RED_DECK, GREEN_DECK, GreedyAgent())
        obs, out = env.reset(0), []
        while not env.done and len(out) < n:
            out.append(obs)
            obs, _, _ = env.step(0)
        return out

    def test_padding_options_get_no_probability(self):
        from optcg.nn.model import collate
        obs = self.observations()
        logits, value = self.net(collate(obs))
        self.assertEqual(value.shape, (len(obs),))
        for i, o in enumerate(obs):
            n = len(o["options"])
            self.assertTrue(torch.isfinite(logits[i, :n]).all())
            self.assertTrue(torch.isinf(logits[i, n:]).all())

    def test_batch_matches_single(self):
        from optcg.nn.model import collate
        obs = self.observations()
        batch, _ = self.net(collate(obs))
        for i in (0, len(obs) - 1):
            one, _ = self.net(collate([obs[i]]))
            n = len(obs[i]["options"])
            self.assertTrue(torch.allclose(batch[i, :n], one[0, :n], atol=1e-5))

    def test_plays_a_full_game(self):
        from optcg.nn.model import NeuralAgent
        g = play_game((RED_DECK, GREEN_DECK), (NeuralAgent(self.net, sample=True), GreedyAgent()), seed=1)
        self.assertTrue(g.over)

    def test_save_and_load(self):
        from optcg.nn.model import collate, load, save
        obs = self.observations(3)
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "net.pt")
            save(self.net, path)
            again = load(path)
        a, _ = self.net(collate(obs))
        b, _ = again(collate(obs))
        self.assertTrue(torch.equal(a, b))

    def test_learns_to_copy_a_choice(self):
        """Gradient steps toward option 0 make option 0 the favourite almost everywhere."""
        from optcg.nn.model import collate
        obs = [o for o in self.observations(40) if len(o["options"]) > 1]
        b = collate(obs)
        opt = torch.optim.Adam(self.net.parameters(), lr=3e-3)
        for _ in range(150):
            logits, _ = self.net(b)
            loss = torch.nn.functional.cross_entropy(logits, torch.zeros(len(obs), dtype=torch.long))
            opt.zero_grad()
            loss.backward()
            opt.step()
        self.assertGreaterEqual((self.net(b)[0].argmax(1) == 0).float().mean().item(), 0.9)

    def test_advantages_with_lambda_one_are_reward_minus_value(self):
        from optcg.nn.train import _advantages
        ep = {"reward": 1.0, "steps": [(None, 0, 0.0, v) for v in (0.1, -0.2, 0.5)]}
        self.assertEqual([round(a, 6) for a in _advantages(ep, 1.0, 1.0)], [0.9, 1.2, 0.5])


if __name__ == "__main__":
    unittest.main()
