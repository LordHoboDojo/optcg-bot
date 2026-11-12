"""Multi-deck league bookkeeping (optcg/nn/league.py): deck files, matchup sampling, PFSP, the
matchup table. The sampler and PFSP are plain Python; the job assignment test needs PyTorch."""
import random
import tempfile
import unittest
from pathlib import Path

from optcg.nn.league import (PFSP, MatchupSampler, format_table, ordered_matchups, read_decks, summary,
                             table)

try:
    import torch
except ImportError:
    torch = None

ROOT = Path(__file__).resolve().parent.parent


class DeckFile(unittest.TestCase):
    def test_league15_lists_15_official_decks(self):
        from optcg.carddb import official_decks
        names = read_decks(ROOT / "decks" / "league15.txt")
        self.assertEqual(len(names), 15)
        self.assertEqual(len(set(names)), 15)
        official = official_decks()
        for n in names:
            self.assertIn(n, official)
        colors = {c for n in names for c in official[n].leader.colors}
        self.assertEqual(colors, {"red", "green", "blue", "purple", "black", "yellow"})

    def test_comments_and_blank_lines_are_skipped(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "decks.txt"
            p.write_text("# a league\n\nA   # first\nB\n  # nothing\nC\n")
            self.assertEqual(read_decks(p), ["A", "B", "C"])
            p.write_text("A\nA\n")
            with self.assertRaises(ValueError):
                read_decks(p)
            p.write_text("A\n")
            with self.assertRaises(ValueError):
                read_decks(p)

    def test_ordered_matchups(self):
        self.assertEqual(len(ordered_matchups(list("abc"))), 9)
        self.assertEqual(set(ordered_matchups(list("ab"), mirrors=False)), {("a", "b"), ("b", "a")})


class Sampler(unittest.TestCase):
    def setUp(self):
        self.ms = ordered_matchups(list("abc"))

    def test_uniform_without_results(self):
        s = MatchupSampler(self.ms)
        for p in s.probs():
            self.assertAlmostEqual(p, 1 / 9)

    def test_smallest_edge_over_greedy_comes_up_most(self):
        s = MatchupSampler(self.ms, floor=0.3, temp=0.1)
        s.set_baseline({m: 0.5 for m in self.ms} | {("a", "b"): 0.8})
        for m in self.ms:
            s.record("greedy", m, 6, 10)  # 60% everywhere: +10% edge...
        # ...except a vs b, where greedy itself wins 80%, so 60% is a -20% edge
        self.assertAlmostEqual(s.edge(("a", "b")), (6 + 4 * 0.8) / 14 - 0.8)
        p = dict(zip(self.ms, s.probs()))
        self.assertEqual(max(p, key=p.get), ("a", "b"))
        self.assertAlmostEqual(sum(p.values()), 1.0)

    def test_floor_keeps_every_matchup_in_play(self):
        s = MatchupSampler(self.ms, floor=0.3, temp=0.01)
        for m in self.ms:
            s.record("greedy", m, 10 if m != ("c", "c") else 0, 10)
        p = s.probs()
        for x in p:
            self.assertGreaterEqual(x, 0.3 / 9 - 1e-12)
        self.assertGreater(p[self.ms.index(("c", "c"))], 0.6)
        draws = s.sample(random.Random(0), 3000)
        self.assertEqual(set(draws), set(self.ms))

    def test_edge_over_best_cancels_deck_strength(self):
        """Even results against the best in both seats of a pairing are no edge at all, however
        lopsided the pairing is."""
        s = MatchupSampler(self.ms)
        s.record("best", ("a", "b"), 90, 100)  # a beats b 90% of the time...
        s.record("best", ("b", "a"), 10, 100)  # ...whoever plays it
        self.assertAlmostEqual(s.edge(("a", "b")), 0.0, delta=0.01)
        s.reset("best")
        s.record("best", ("a", "b"), 100, 100)
        s.record("best", ("b", "a"), 50, 100)
        self.assertGreater(s.edge(("a", "b")), 0.4)
        self.assertEqual(s.edge(("a", "b")), s.edge(("b", "a")))

    def test_other_opponents_dont_count(self):
        s = MatchupSampler(self.ms)
        s.record("iter10", ("a", "b"), 0, 50)
        self.assertEqual(s.edge(("a", "b")), 0.0)

    def test_old_results_decay(self):
        s = MatchupSampler(self.ms, decay=0.5)
        s.record("greedy", ("a", "a"), 0, 8)
        before = s.edge(("a", "a"))
        s.step()
        s.step()
        self.assertGreater(s.edge(("a", "a")), before)  # pulled back towards no edge
        self.assertLess(s.edge(("a", "a")), 0)


class Pfsp(unittest.TestCase):
    def test_opponents_that_beat_the_learner_come_up_most(self):
        f = PFSP(power=2.0)
        for _ in range(20):
            f.record("weak", 1.0)
            f.record("strong", 0.0)
        w = dict(zip(["weak", "even", "strong"], f.weights(["weak", "even", "strong"])))
        self.assertAlmostEqual(sum(w.values()), 1.0)
        self.assertGreater(w["strong"], w["even"])
        self.assertGreater(w["even"], w["weak"])
        self.assertGreater(w["weak"], 0)  # never dropped altogether

    def test_power_zero_is_uniform(self):
        f = PFSP(power=0.0)
        f.record("a", 1.0)
        self.assertEqual(f.weights(["a", "b"]), [0.5, 0.5])


class Table(unittest.TestCase):
    def test_table_and_edge(self):
        decks = ["(Red) A", "(Blue) B"]
        res = {("(Red) A", "(Red) A"): [3, 4], ("(Red) A", "(Blue) B"): [1, 4], ("(Blue) B", "(Red) A"): [4, 4]}
        base = {m: [2, 4] for m in ordered_matchups(decks)}
        t = table(decks, res, base, iteration=3)
        self.assertEqual(t["win"], [[0.75, 0.25], [1.0, None]])
        self.assertEqual(t["edge"], [[0.25, -0.25], [0.5, None]])
        self.assertAlmostEqual(t["mean_win"], 2 / 3)
        self.assertAlmostEqual(t["mean_edge"], 0.5 / 3)
        self.assertEqual(t["games"], 12)
        self.assertEqual(t["iteration"], 3)
        text = format_table(t)
        self.assertIn("A", text)
        self.assertIn("  100", text)
        self.assertIn("edge over greedy", summary(t))

    def test_render_tool(self):
        import io
        import json
        from contextlib import redirect_stdout

        sys_path = str(ROOT / "tools")
        import sys
        sys.path.insert(0, sys_path)
        try:
            import matchup_table
        finally:
            sys.path.remove(sys_path)
        decks = ["(Red) A", "(Blue) B"]
        t = table(decks, {m: [1, 2] for m in ordered_matchups(decks)}, {m: [1, 2] for m in ordered_matchups(decks)})
        with tempfile.TemporaryDirectory() as d:
            p, h = Path(d) / "t.json", Path(d) / "t.html"
            p.write_text(json.dumps(t))
            out = io.StringIO()
            with redirect_stdout(out):
                matchup_table.main([str(p), "--edge", "--html", str(h)])
            self.assertIn("mean edge over greedy +0.0%", out.getvalue())
            self.assertIn("<table>", h.read_text())


@unittest.skipIf(torch is None, "PyTorch not installed")
class Assignment(unittest.TestCase):
    def test_games_split_between_greedy_best_and_snapshots(self):
        from optcg.nn.train import assign_games
        ms = ordered_matchups(list("abc"))
        sampler, pfsp = MatchupSampler(ms), PFSP()
        snaps = {"iter10": {}, "iter20": {}}
        for _ in range(30):
            pfsp.record("iter10", 1.0)
        jobs = assign_games(random.Random(0), 4000, sampler, snaps, pfsp, 0.25, True, 0.1, 100)
        self.assertEqual([j[0] for j in jobs], list(range(100, 4100)))
        opp = [j[4] for j in jobs]
        self.assertAlmostEqual(opp.count("greedy") / len(opp), 0.25, delta=0.03)
        self.assertAlmostEqual(opp.count("best") / len(opp), 0.10, delta=0.02)
        self.assertGreater(opp.count("iter20"), 3 * opp.count("iter10"))  # PFSP: iter10 always loses
        self.assertEqual({j[3] for j in jobs}, {0, 1})
        self.assertEqual({(j[1], j[2]) for j in jobs}, set(ms))

    def test_greedy_takes_the_games_nothing_else_can(self):
        from optcg.nn.train import assign_games
        ms = ordered_matchups(list("ab"))
        jobs = assign_games(random.Random(1), 200, MatchupSampler(ms), {}, PFSP(), 0.25, False, 0.1, 0)
        self.assertEqual({j[4] for j in jobs}, {"greedy"})
        jobs = assign_games(random.Random(1), 400, MatchupSampler(ms), {}, PFSP(), 0.25, True, 0.1, 0)
        self.assertEqual({j[4] for j in jobs}, {"greedy", "best"})


if __name__ == "__main__":
    unittest.main()
