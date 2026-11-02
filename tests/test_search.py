"""The search bot (optcg/search.py) and the Rust engine pieces it stands on.
Run with: python3 -m unittest discover tests"""
import json
import random
import tempfile
import unittest
from pathlib import Path

from optcg.env import rust_available

if rust_available():
    import torch

    from optcg.env import rust_game
    from optcg.nn.model import PolicyNet, save
    from optcg.play import resolve_deck
    from optcg.search import MCTSAgent

    DECKS = ("rocks", "(Yellow) Charlotte Linlin")


def _game(seed, fast=True):
    return rust_game(tuple(resolve_deck(s) for s in DECKS), seed, fast=fast)


def _hands(g):
    return [{u: d for u, d, *_ in p["hand"]} for p in json.loads(g.snapshot_json())["players"]]


@unittest.skipUnless(rust_available(), "the Rust engine is not built (engine-rs/)")
class Determinize(unittest.TestCase):
    def test_option_keys_mean_the_same_in_every_world(self):
        """The searcher's own options get the same names in each world it imagines."""
        rng = random.Random(0)
        checked = 0
        for seed in range(6):
            g = _game(seed)
            while not g.over:
                p = g.pending_player()
                for s in range(3):
                    w = g.fork()
                    if w.determinize(p, s):
                        self.assertEqual(w.option_keys(p), g.option_keys(p))
                        checked += 1
                g.step(rng.randrange(g.num_options()))
        self.assertGreater(checked, 500)

    def test_a_revealed_card_stays_in_the_hand(self):
        """A card added to the hand face up (Linlin's search) is known to the opponent, so the
        worlds they imagine keep it there."""
        rng = random.Random(1)
        for seed in range(300):
            g = _game(seed, fast=False)
            revealed = None
            while not g.over:
                before = _hands(g)
                n = g.log_len
                g.step(rng.randrange(g.num_options()))
                for line in g.log[n:]:
                    if " reveals " in line and "adds it to their hand" in line:
                        p = int(line.split("Player ")[1][0])
                        new = set(_hands(g)[p]) - set(before[p])
                        if len(new) == 1:
                            revealed = (p, new.pop())
                if revealed and not g.over and revealed[1] in _hands(g)[revealed[0]]:
                    p, uid = revealed
                    other = 1 - p
                    card = _hands(g)[p][uid]
                    moved = 0
                    for s in range(10):
                        w = g.fork()
                        if w.determinize(other, s):
                            self.assertEqual(_hands(w)[p][uid], card)
                            moved += _hands(w)[p] != _hands(g)[p]
                    if moved:  # the rest of that hand was reshuffled, but not this card
                        return
        self.skipTest("no revealed card came up in these games")


@unittest.skipUnless(rust_available(), "the Rust engine is not built (engine-rs/)")
class Search(unittest.TestCase):
    def setUp(self):
        torch.manual_seed(0)
        self.bot = MCTSAgent(PolicyNet(32), sims=24, worlds=4, batch=4, seed=0)

    def test_plays_whole_games_with_legal_moves(self):
        for seed in range(2):
            g = _game(seed)
            while not g.over:
                if g.pending_player() == 0:
                    i = self.bot.pick(g)
                    self.assertIn(i, range(g.num_options()))
                    g.step(i)
                else:
                    g.step(g.greedy())

    def test_web_game_keeps_its_rust_copy_in_step(self):
        from optcg.web import Session
        with tempfile.TemporaryDirectory() as d:
            path = str(Path(d) / "net.pt")
            torch.manual_seed(0)
            save(PolicyNet(32), path)
            s = Session(tuple(resolve_deck(x) for x in DECKS), f"mcts:{path}", human=0, seed=4)
            s.bot.sims, s.bot.worlds, s.bot.batch = 16, 4, 4
            for _ in range(40):
                if s.g.over:
                    break
                s.act(len(s.frames) - 1, random.Random(len(s.frames)).randrange(len(s.g.pending.options)))
                self.assertEqual(s.rg.num_options(), len(s.g.pending.options) if s.g.pending else 0)


@unittest.skipUnless(rust_available(), "the Rust engine is not built (engine-rs/)")
class LearnFromSearch(unittest.TestCase):
    def test_visit_shares_are_a_distribution_over_the_options(self):
        bot = MCTSAgent(PolicyNet(32), sims=24, worlds=4, batch=4, seed=0, noise=0.25)
        g = _game(2)
        rng = random.Random(2)
        seen = 0
        while not g.over and seen < 10:
            if g.num_options() > 1:
                visits, prior = bot.policy(g)
                self.assertEqual(len(visits), g.num_options())
                self.assertAlmostEqual(sum(visits), 1.0, places=6)
                self.assertTrue(all(v >= 0 for v in visits))
                seen += 1
            g.step(rng.randrange(g.num_options()))

    def test_a_short_run_learns_and_keeps_the_best(self):
        from optcg.nn.distill import main
        with tempfile.TemporaryDirectory() as d:
            torch.manual_seed(0)
            ref = str(Path(d) / "ref.pt")
            save(PolicyNet(32), ref)
            main(["--init", ref, "--reference", ref, "--out", d, "--iters", "2", "--games", "2", "--sims", "8",
                  "--worlds", "2", "--eval-every", "1", "--eval-games", "4", "--workers", "1", "--device", "cpu"])
            best = torch.load(Path(d) / "best.pt", weights_only=False)
            scores = [torch.load(Path(d) / f"iter{i:04d}.pt", weights_only=False)["eval_vs_reference"] for i in (1, 2)]
            self.assertEqual(best["eval_vs_reference"], max(scores))


if __name__ == "__main__":
    unittest.main()
