"""Training setup: deck pools, seat assignment, advantages. Skipped without PyTorch."""
import argparse
import unittest

try:
    import torch
except ImportError:
    torch = None


@unittest.skipIf(torch is None, "PyTorch not installed")
class Matchups(unittest.TestCase):
    def setUp(self):
        from optcg.nn.train import covered_decks, matchups
        self.covered_decks, self.matchups = covered_decks, matchups

    def args(self, league):
        return argparse.Namespace(league=league, deck0="sample-red", deck1="sample-green")

    def test_single_matchup_without_league(self):
        self.assertEqual(self.matchups(self.args(False)), (("sample-red", "sample-green"),))

    def test_league_pairs_every_fully_covered_deck(self):
        names = self.covered_decks()
        pairs = self.matchups(self.args(True))
        self.assertEqual(len(pairs), len(names) * (len(names) - 1) // 2)
        self.assertEqual({n for p in pairs for n in p}, set(names))
        self.assertEqual(len(set(pairs)), len(pairs))

    def test_learner_plays_both_sides_of_every_matchup(self):
        """Each pairing must come up with the learner on either side, or it only learns one deck of it."""
        from optcg.nn.train import _assign
        pairs = self.matchups(self.args(True))
        seats = {}
        played = set()
        for seed in range(12 * len(pairs)):
            matchup, seat = _assign(pairs, seed)
            seats.setdefault(matchup, set()).add(seat)
            played.add(matchup[seat])
        self.assertEqual(set(seats), set(pairs))
        for matchup, got in seats.items():
            self.assertEqual(got, {0, 1}, f"{matchup} only ever ran seat {got}")
        self.assertEqual(played, set(self.covered_decks()))

    def test_assignment_is_stable_for_a_seed(self):
        from optcg.nn.train import _assign
        pairs = self.matchups(self.args(True))
        self.assertEqual(_assign(pairs, 41), _assign(pairs, 41))


@unittest.skipIf(torch is None, "PyTorch not installed")
class BatchedPlay(unittest.TestCase):
    """Several games per worker, with the network's moves scored in one batch."""

    def setUp(self):
        from optcg.env import rust_available
        from optcg.nn.model import PolicyNet
        torch.manual_seed(0)
        self.net = PolicyNet(hidden=32).eval()
        self.state = self.net.state_dict()
        self.engine = "rust" if rust_available() else "python"
        self.pairs = (("(Blue) Rocks.D.Xebec", "(Yellow) Charlotte Linlin"),)

    def test_batched_eval_matches_one_at_a_time(self):
        """Best-move play is deterministic, so batching must not change a single result."""
        from optcg.nn.train import _eval_games
        seeds = list(range(12))
        for opponent in ("greedy", self.state):
            one = _eval_games((self.pairs, self.state, opponent, 32, seeds, self.engine, 1))
            many = _eval_games((self.pairs, self.state, opponent, 32, seeds, self.engine, 8))
            self.assertEqual(one, many)

    def test_rollouts_record_every_learner_decision(self):
        from optcg.nn.model import collate
        from optcg.nn.train import _rollouts
        seeds = list(range(6))
        eps = _rollouts((self.pairs, self.state, 32, ["greedy", self.state], seeds, self.engine, 4))
        self.assertEqual(len(eps), len(seeds))
        for e in eps:
            self.assertIn(e["reward"], (-1.0, 0.0, 1.0))
            obs = [s[0] for s in e["steps"]]
            self.assertTrue(all(0 <= a < len(o["options"]) for o, a, _, _ in e["steps"]))
            with torch.no_grad():
                logits, values = self.net(collate(obs))
            lp = torch.distributions.Categorical(logits=logits).log_prob(torch.tensor([s[1] for s in e["steps"]]))
            self.assertTrue(torch.allclose(lp, torch.tensor([s[2] for s in e["steps"]]), atol=1e-4))
            self.assertTrue(torch.allclose(values, torch.tensor([s[3] for s in e["steps"]]), atol=1e-4))


@unittest.skipIf(torch is None, "PyTorch not installed")
class SelfPlay(unittest.TestCase):
    def test_greedy_gets_its_share_of_the_games(self):
        from optcg.nn.train import _opponents
        snaps = [{"n": i} for i in range(30)]
        for share in (0.0, 0.25, 0.5):
            pool = _opponents(snaps, 20, share)
            self.assertEqual(pool[-20:], snaps[-20:])  # the most recent snapshots
            self.assertAlmostEqual(pool.count("greedy") / len(pool), share, delta=0.03)
        self.assertEqual(_opponents([], 20, 0.25), ["greedy"])  # nothing else to play yet

    def test_judged_against_a_reference_and_the_best_is_kept(self):
        import tempfile
        from pathlib import Path

        from optcg.nn.model import PolicyNet, save
        from optcg.nn.train import main
        with tempfile.TemporaryDirectory() as d:
            torch.manual_seed(0)
            ref = str(Path(d) / "ref.pt")
            save(PolicyNet(hidden=32), ref)
            main(["ppo", "--init", ref, "--reference", ref,
                  "--out", d, "--iters", "2", "--games", "4", "--eval-every", "1", "--eval-games", "4",
                  "--workers", "1", "--device", "cpu", "--hidden", "32"])
            best = torch.load(Path(d) / "best.pt", weights_only=False)
            self.assertIn("eval_vs_reference", best)
            scores = [torch.load(Path(d) / f"iter{i:04d}.pt", weights_only=False)["eval_vs_reference"]
                      for i in (1, 2)]
            self.assertEqual(best["eval_vs_reference"], max(scores))


if __name__ == "__main__":
    unittest.main()
