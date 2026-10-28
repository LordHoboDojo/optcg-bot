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


if __name__ == "__main__":
    unittest.main()
