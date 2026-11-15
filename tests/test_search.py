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

    def test_web_game_plays_the_search_bot(self):
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
                s.act(len(s.frames) - 1, random.Random(len(s.frames)).randrange(s.g.num_options()))
                self.assertTrue(s.g.over or s.g.pending_player() == 0)
            self.assertTrue(any(f["d"]["pl"] == 1 for f in s.frames if "d" in f))


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
            if best["iteration"] == 0:  # the starting checkpoint held on
                self.assertGreaterEqual(best["eval_vs_reference"], max(scores))
            else:
                self.assertEqual(best["eval_vs_reference"], max(scores))

    def test_league_run_tables_best_and_resume(self):
        """distill --decks end to end on a 3-deck league: the starting checkpoint is judged first,
        each evaluation saves a matchup table and plays the reference head-to-head, best.pt keeps
        the best of them, and --resume carries on from latest.pt's iteration and optimizer."""
        import io
        from contextlib import redirect_stdout

        from optcg.nn.distill import main
        from optcg.nn.model import conditioned, load
        with tempfile.TemporaryDirectory() as d:
            torch.manual_seed(0)
            ref = str(Path(d) / "ref.pt")
            save(conditioned(32), ref)
            decks = Path(d) / "decks.txt"
            decks.write_text("(Blue) Rocks.D.Xebec\n(Yellow) Charlotte Linlin\n(Red) Roronoa Zoro\n", encoding="utf-8")
            run = ["--decks", str(decks), "--reference", ref, "--out", d, "--games", "3", "--sims", "8",
                   "--worlds", "2", "--eval-every", "1", "--eval-games", "6", "--table-games", "2",
                   "--baseline-games", "2", "--workers", "1", "--device", "cpu"]
            out = io.StringIO()
            with redirect_stdout(out):
                main(run + ["--init", ref, "--iters", "2"])
            log = out.getvalue()
            self.assertIn("iteration 0", log)
            self.assertIn("iter 2: 3 games", log)
            t = json.loads((Path(d) / "table_latest.json").read_text(encoding="utf-8"))
            self.assertEqual(t["games"], 18)
            self.assertEqual(t["iteration"], 2)
            self.assertTrue((Path(d) / "table_iter0000.json").is_file())
            best = torch.load(Path(d) / "best.pt", weights_only=False)
            scores = [json.loads((Path(d) / f"table_iter{i:04d}.json").read_text(encoding="utf-8")) for i in (0, 1, 2)]
            evals = [best["eval_vs_reference"]] + [
                torch.load(Path(d) / f"iter{i:04d}.pt", weights_only=False)["eval_vs_reference"] for i in (1, 2)]
            self.assertEqual(best["eval_vs_reference"], max(evals))
            self.assertIn("mean_edge", best)
            self.assertEqual(len(scores), 3)
            self.assertTrue(load(str(Path(d) / "best.pt")).deck_cond)
            ck = torch.load(Path(d) / "latest.pt", weights_only=False)
            self.assertEqual(ck["iteration"], 2)
            self.assertIn("state", ck["optimizer"])
            out = io.StringIO()
            with redirect_stdout(out):
                main(run + ["--iters", "3", "--resume"])
            log = out.getvalue()
            self.assertIn("after iteration 2 with its saved optimizer state", log)
            self.assertIn("matchup sampler seeded from", log)
            self.assertNotIn("iteration 0", log)
            self.assertNotIn("iter 2:", log)
            self.assertIn("iter 3:", log)
            with self.assertRaises(SystemExit):
                main(run + ["--iters", "3", "--resume"])

    def test_self_play_records_both_players_and_the_result(self):
        from optcg.nn.distill import _search_games
        from optcg.nn.model import conditioned
        torch.manual_seed(0)
        net = conditioned(32)
        jobs = [(5, ("(Blue) Rocks.D.Xebec", "(Purple) Kaido"))]
        samples, games = _search_games((net.state_dict(), net.config(), jobs, dict(sims=8, worlds=2), 4))
        self.assertEqual(len(games), 1)
        decks, winner, n = games[0]
        self.assertEqual(decks, jobs[0][1])
        self.assertEqual(len(samples), n)
        self.assertEqual({s[0]["player"] for s in samples}, {0, 1})
        for obs, visits, result in samples:
            self.assertEqual(len(visits), len(obs["options"]))
            self.assertAlmostEqual(sum(visits), 1.0, places=6)
            want = 0.0 if winner is None else (1.0 if winner == obs["player"] else -1.0)
            self.assertEqual(result, want)

    def test_equal_networks_tie_exactly_head_to_head(self):
        """Each deal is played both ways round, so a network against itself scores exactly 1/2."""
        from optcg.nn.distill import head_to_head
        from optcg.nn.model import conditioned
        from optcg.nn.train import _pool
        torch.manual_seed(0)
        net = conditioned(32)
        pairings = [("(Blue) Rocks.D.Xebec", "(Purple) Kaido"), ("(Purple) Kaido", "(Blue) Rocks.D.Xebec")]
        with _pool(1) as pool:
            score, by_deck, n = head_to_head(pool, 1, pairings, net.state_dict(), net.state_dict(), net.config(), 8)
        self.assertEqual(n, 8)
        self.assertEqual(score, 0.5)
        self.assertEqual({d for d in by_deck}, {"(Blue) Rocks.D.Xebec", "(Purple) Kaido"})


class Jobs(unittest.TestCase):
    """Which games a league run and a league comparison play (no engine needed)."""

    def test_distill_games_follow_the_sampler_from_both_seats(self):
        from optcg.nn.distill import game_jobs
        from optcg.nn.league import MatchupSampler, ordered_matchups
        ms = ordered_matchups(list("abc"))
        s = MatchupSampler(ms, floor=0.2, temp=0.05)
        s.set_baseline({m: 0.5 for m in ms})
        for m in ms:
            s.record("greedy", m, 8 if m != ("a", "c") else 1, 10)
        jobs = game_jobs(random.Random(0), 3000, s, 100)
        self.assertEqual([j[0] for j in jobs], list(range(100, 3100)))
        seated = {decks for _, decks in jobs}
        self.assertEqual(seated, set(ms))  # every matchup, in both seat orders
        a_c = sum(decks in (("a", "c"), ("c", "a")) for _, decks in jobs)
        self.assertGreater(a_c / len(jobs), 0.5)

    def test_league_comparison_plays_every_matchup_from_both_seats(self):
        from optcg.nn.league import ordered_matchups
        from optcg.search import jobs_for
        decks = list("abc")
        jobs = jobs_for(decks, "x", "y", 36, 7)
        self.assertEqual([j[0] for j in jobs], list(range(7, 43)))
        seen = {}
        for _, seated, seat in jobs:
            mine, theirs = seated[seat], seated[1 - seat]
            seen.setdefault((mine, theirs), set()).add(seat)
        self.assertEqual(set(seen), set(ordered_matchups(decks)))
        self.assertTrue(all(s == {0, 1} for s in seen.values()))
        self.assertEqual(jobs_for(None, "x", "y", 3, 0), [(0, ("x", "y"), 0), (1, ("x", "y"), 1), (2, ("x", "y"), 0)])


    def test_head_to_head_deals_come_in_pairs(self):
        from optcg.nn.distill import paired_jobs
        from optcg.nn.league import ordered_matchups
        from optcg.nn.train import _seated
        pairings = ordered_matchups(list("abc"))
        jobs = paired_jobs(pairings, 36, 50)
        self.assertEqual(len(jobs), 36)
        for first, second in zip(jobs[::2], jobs[1::2]):
            self.assertEqual(first[0], second[0])  # same deal
            self.assertEqual(_seated(*first[1:4]), _seated(*second[1:4]))  # same decks in the same seats
            self.assertEqual(first[3], 1 - second[3])  # a holds the other seat's deck
        self.assertEqual({(j[1], j[2]) for j in jobs}, set(pairings))


if __name__ == "__main__":
    unittest.main()
