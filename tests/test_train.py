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

    def args(self, league=False, decks=None):
        return argparse.Namespace(league=league, decks=decks, deck0="sample-red", deck1="sample-green")

    def test_one_matchup_from_both_sides(self):
        self.assertEqual(self.matchups(self.args()), [("sample-red", "sample-green"), ("sample-green", "sample-red")])

    def test_league_pairs_every_fully_covered_deck_both_ways(self):
        names = self.covered_decks()
        pairs = self.matchups(self.args(league=True))
        self.assertEqual(len(pairs), len(names) * (len(names) - 1))
        self.assertEqual({n for p in pairs for n in p}, set(names))
        self.assertEqual(len(set(pairs)), len(pairs))

    def test_deck_file_gives_every_ordered_matchup_with_mirrors(self):
        from pathlib import Path
        path = str(Path(__file__).resolve().parent.parent / "decks" / "league15.txt")
        pairs = self.matchups(self.args(decks=path))
        self.assertEqual(len(pairs), 225)
        self.assertIn(("(Blue) Rocks.D.Xebec", "(Blue) Rocks.D.Xebec"), pairs)

    def test_round_robin_covers_both_seats_of_every_matchup(self):
        """Evaluations must play each matchup from both seats, or one side's first-turn edge sticks."""
        from optcg.nn.train import _round_robin
        pairs = self.matchups(self.args(league=True))[:7]
        jobs = _round_robin(pairs, 4 * len(pairs), 100)
        self.assertEqual([j[0] for j in jobs], list(range(100, 100 + 4 * len(pairs))))
        seats = {}
        for _, mine, theirs, seat, _ in jobs:
            seats.setdefault((mine, theirs), set()).add(seat)
        self.assertEqual(set(seats), set(pairs))
        self.assertTrue(all(s == {0, 1} for s in seats.values()))


@unittest.skipIf(torch is None, "PyTorch not installed")
class BatchedPlay(unittest.TestCase):
    """Several games per worker, with the network's moves scored in one batch."""

    def setUp(self):
        from optcg.env import rust_available
        from optcg.nn.model import PolicyNet
        torch.manual_seed(0)
        self.net = PolicyNet(hidden=32).eval()
        self.state = self.net.state_dict()
        self.config = self.net.config()
        self.engine = "rust" if rust_available() else "python"
        rocks, linlin = "(Blue) Rocks.D.Xebec", "(Yellow) Charlotte Linlin"
        self.jobs = [(s, rocks, linlin, s % 2, "greedy" if s % 3 else "snap") for s in range(12)]

    def test_batched_eval_matches_one_at_a_time(self):
        """Best-move play is deterministic, so batching must not change a single result."""
        from optcg.nn.train import _eval_games
        for opponent in ("greedy", self.state):
            one = _eval_games((self.jobs, self.state, opponent, self.config, self.engine, 1))
            many = _eval_games((self.jobs, self.state, opponent, self.config, self.engine, 8))
            self.assertEqual(one, many)
            self.assertEqual([(m, s) for m, s, _ in one], [((j[1], j[2]), j[3]) for j in self.jobs])

    def test_rollouts_record_every_learner_decision(self):
        from optcg.nn.model import collate
        from optcg.nn.train import _rollouts
        jobs = self.jobs[:6]
        eps = _rollouts((jobs, self.state, self.config, {"greedy": "greedy", "snap": self.state}, self.engine, 4))
        self.assertEqual(len(eps), len(jobs))
        self.assertEqual([e["opponent"] for e in eps], [j[4] for j in jobs])
        for e, j in zip(eps, jobs):
            self.assertEqual(e["matchup"], (j[1], j[2]))
            self.assertTrue(all(s[0]["player"] == j[3] for s in e["steps"]))  # the learner's own seat
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

    def test_league_smoke_run_writes_matchup_tables(self):
        """ppo --decks end to end on a 3-deck league: baseline, sampled games, the table, best.pt."""
        import json
        import tempfile
        from pathlib import Path

        from optcg.nn.model import load
        from optcg.nn.train import main
        with tempfile.TemporaryDirectory() as d:
            decks = Path(d) / "decks.txt"
            decks.write_text("(Blue) Rocks.D.Xebec\n(Yellow) Charlotte Linlin\n(Red) Roronoa Zoro\n", encoding="utf-8")
            main(["ppo", "--decks", str(decks), "--out", d, "--iters", "2", "--games", "6", "--eval-every", "1",
                  "--table-games", "2", "--baseline-games", "2", "--snapshot-every", "1", "--workers", "1",
                  "--device", "cpu", "--hidden", "32"])
            t = json.loads((Path(d) / "table_latest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(t["win"]), 3)
            self.assertEqual(t["games"], 18)
            self.assertIn("mean_edge", t)
            self.assertTrue((Path(d) / "greedy_baseline.json").is_file())
            self.assertTrue(load(str(Path(d) / "best.pt")).deck_cond)

    def test_exploiters_and_nash_ranking_end_to_end(self):
        """Phase 2 on a 2-deck league: both exploiter kinds play, get promoted into the main agent's
        pool, members are ranked by the Nash meta-game, and --resume picks all of it up."""
        import io
        import json
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path

        from optcg.nn.model import conditioned, save
        from optcg.nn.train import main
        with tempfile.TemporaryDirectory() as d:
            torch.manual_seed(0)
            decks = Path(d) / "decks.txt"
            decks.write_text("(Blue) Rocks.D.Xebec\n(Yellow) Charlotte Linlin\n", encoding="utf-8")
            init = str(Path(d) / "init.pt")
            save(conditioned(32), init)
            target = Path(d) / "target.json"
            target.write_text(json.dumps({"decks": ["(Blue) Rocks.D.Xebec", "(Yellow) Charlotte Linlin"],
                                          "edge": [[0.1, -0.4], [0.0, 0.2]]}), encoding="utf-8")
            run = ["ppo", "--decks", str(decks), "--init", init, "--out", d, "--games", "4", "--eval-every", "1",
                   "--table-games", "2", "--baseline-games", "2", "--snapshot-every", "1", "--workers", "1",
                   "--device", "cpu", "--main-exploiters", "1", "--league-exploiters", "1",
                   "--exploiter-games", "4", "--exploiter-min-iters", "1", "--promote-at", "0",
                   "--league-reset", "0", "--exploit-targets", str(target), "--exploit-cells", "2", "--nash", "--nash-games", "2"]
            out = io.StringIO()
            with redirect_stdout(out):
                main(run + ["--iters", "2"])
            log = out.getvalue()
            self.assertIn("2 target matchups", log)  # the 2 weakest cells of the 2x2 table
            self.assertIn("mx1.g1 joins the main agent's opponents", log)
            self.assertIn("lx1.g2 joins", log)
            self.assertIn("Nash ranking", log)
            saved = json.loads((Path(d) / "league.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["copies"], ["mx1.g1", "lx1.g1", "mx1.g2", "lx1.g2"])
            members = [m for m, _ in saved["meta"]["members"]]
            self.assertIn("greedy", members)
            self.assertIn("iter2", members)
            self.assertIn(saved["best"], members)
            for f in ("mx1.pt", "lx1.pt", "mx1.g1.pt", "best.pt"):
                self.assertTrue((Path(d) / f).is_file(), f)
            self.assertEqual(torch.load(Path(d) / "mx1.pt", weights_only=False)["gen"], 3)
            out = io.StringIO()
            with redirect_stdout(out):
                main(run + ["--iters", "3", "--resume"])
            log = out.getvalue()
            self.assertIn("generation 3", log)
            self.assertIn("mx1.g3 joins", log)
            self.assertIn("iter 3:", log)
            saved = json.loads((Path(d) / "league.json").read_text(encoding="utf-8"))
            self.assertIn("mx1.g3", saved["copies"])

    def test_exploiter_games(self):
        import argparse
        import random

        from optcg.nn.model import conditioned
        from optcg.nn.train import Exploiter
        args = argparse.Namespace(lr=1e-4, decay=0.95, pfsp_power=2.0, exploit_focus=1.0, exploiter_min_iters=2,
                                  promote_at=0.7, exploiter_budget=5)
        net = conditioned(16)
        state, config = net.state_dict(), net.config()
        pairings = [(a, b) for a in "xyz" for b in "xyz"]
        mx = Exploiter("mx1", "main", state, config, "cpu", args)
        jobs = mx.jobs(random.Random(0), 50, pairings, [("x", "y")], ["iter10"], 7)
        self.assertEqual({(j[1], j[2]) for j in jobs}, {("y", "x")})  # the opponent's deck vs main's x
        self.assertEqual({j[4] for j in jobs}, {"main"})
        self.assertEqual(jobs[0][0], 7)
        lx = Exploiter("lx1", "league", state, config, "cpu", args)
        for _ in range(30):
            lx.pfsp.record("iter10", 1.0)
        jobs = lx.jobs(random.Random(0), 400, pairings, [], ["iter10", "mx1.g1"], 0)
        keys = [j[4] for j in jobs]
        self.assertGreater(keys.count("mx1.g1"), 3 * keys.count("iter10"))
        self.assertEqual(lx.jobs(random.Random(0), 3, pairings, [], [], 0)[0][4], "main")
        self.assertFalse(mx.ready())
        mx.iters = 2
        mx.rates.record("all", 10, 10)
        self.assertTrue(mx.ready())
        mx.rates.counts.clear()
        self.assertFalse(mx.ready())
        mx.iters = 5
        self.assertTrue(mx.ready())

    def test_resume_continues_a_run_from_its_files(self):
        """--resume picks up after latest.pt's iteration with its optimizer, the snapshots and the best."""
        import io
        import tempfile
        from contextlib import redirect_stdout
        from pathlib import Path

        from optcg.nn.train import main
        with tempfile.TemporaryDirectory() as d:
            decks = Path(d) / "decks.txt"
            decks.write_text("(Blue) Rocks.D.Xebec\n(Yellow) Charlotte Linlin\n", encoding="utf-8")
            run = ["ppo", "--decks", str(decks), "--out", d, "--games", "4", "--eval-every", "1",
                   "--table-games", "2", "--baseline-games", "2", "--snapshot-every", "1", "--workers", "1",
                   "--device", "cpu", "--hidden", "32"]
            main(run + ["--iters", "2"])
            ck = torch.load(Path(d) / "latest.pt", weights_only=False)
            self.assertEqual(ck["iteration"], 2)
            self.assertIn("state", ck["optimizer"])
            out = io.StringIO()
            with redirect_stdout(out):
                main(run + ["--iters", "3", "--resume"])
            log = out.getvalue()
            self.assertIn("after iteration 2: 2 snapshots", log)
            self.assertIn("saved optimizer state", log)
            self.assertNotIn("iter 1:", log)
            self.assertIn("iter 3:", log)
            self.assertEqual(torch.load(Path(d) / "latest.pt", weights_only=False)["iteration"], 3)
            with self.assertRaises(SystemExit):
                main(run + ["--iters", "3", "--resume"])


if __name__ == "__main__":
    unittest.main()
