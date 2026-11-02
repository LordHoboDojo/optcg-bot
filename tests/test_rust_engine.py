"""The Rust engine (engine-rs/, module optcg_rs) against the Python one. Skipped when the module
isn't built: cd engine-rs && maturin develop --release"""
import itertools
import json
import random
import unittest

try:
    import optcg_rs
except ImportError:
    optcg_rs = None

if optcg_rs is not None:
    from rust_parity import covered, play_both, rust_db


@unittest.skipIf(optcg_rs is None, "optcg_rs not built")
class Parity(unittest.TestCase):
    def test_compiler_matches_on_every_card(self):
        from optcg.carddb import load_cards
        db = rust_db()
        for cid, d in load_cards().items():
            self.assertEqual(list(d.unimplemented), db.unimplemented(cid), cid)
            self.assertEqual(len(d.effects), db.effect_count(cid), cid)

    def test_games_match_decision_by_decision(self):
        """State, options, prompts and log identical after every decision, both seats, all matchups."""
        ds = covered()
        rng = random.Random(3)
        for (a, b) in itertools.product(sorted(ds), repeat=2):
            for policy in ("random", "greedy"):
                for seed in range(3):
                    with self.subTest(a=a, b=b, policy=policy, seed=seed):
                        play_both((ds[a], ds[b]), seed, policy, rng)


@unittest.skipIf(optcg_rs is None, "optcg_rs not built")
class Training(unittest.TestCase):
    """The Rust-backed Env is the same environment: same observations, opponent moves and rewards."""

    def test_env_matches_python(self):
        from optcg.agents import GreedyAgent, RandomAgent
        from optcg.env import Env
        ds = covered()
        names = sorted(ds)
        for seed in range(6):
            decks = (ds[names[seed % 3]], ds[names[(seed + 1) % 3]])
            seat = seed % 2
            opp = GreedyAgent() if seed < 3 else RandomAgent(seed)
            twin = GreedyAgent() if seed < 3 else RandomAgent(seed)
            py, rs = Env(*decks, opponent=opp, seat=seat), Env(*decks, opponent=twin, seat=seat, engine="rust")
            a, b = py.reset(seed), rs.reset(seed)
            rng = random.Random(seed)
            while a is not None:
                self.assertEqual(a, {k: [list(r) for r in v] if k in ("cards", "options") else v for k, v in b.items()})
                k = rng.randrange(len(a["options"]))
                (a, ra, da), (b, rb, db_) = py.step(k), rs.step(k)
                self.assertEqual((ra, da), (rb, db_))
            self.assertIsNone(b)
            self.assertEqual(py.reward(), rs.reward())

    def test_imitation_data_matches_python(self):
        from optcg.nn.train import _imitation_games
        pairs = (("(Blue) Rocks.D.Xebec", "(Black) Monkey.D.Luffy"),)
        py = _imitation_games((pairs, [0, 1], "python"))
        rs = _imitation_games((pairs, [0, 1], "rust"))
        self.assertEqual(len(py), len(rs))
        for (o1, k1), (o2, k2) in zip(py, rs):
            self.assertEqual(k1, k2)
            self.assertEqual(o1["cards"], [list(r) for r in o2["cards"]])


@unittest.skipIf(optcg_rs is None, "optcg_rs not built")
class Search(unittest.TestCase):
    def setUp(self):
        ds = covered()
        decks = [ds["(Blue) Rocks.D.Xebec"], ds["(Yellow) Charlotte Linlin"]]
        self.g = optcg_rs.Game(rust_db(), [(d.leader.id, [c.id for c in d.cards]) for d in decks], 5)
        rng = random.Random(5)
        for _ in range(60):
            self.g.step(rng.randrange(self.g.num_options()))

    def test_clone_is_independent(self):
        before = self.g.snapshot_json()
        c = self.g.clone()
        rng = random.Random(0)
        while not c.over:
            c.step(rng.randrange(c.num_options()))
        self.assertEqual(self.g.snapshot_json(), before)
        self.assertFalse(self.g.over)

    def test_determinize_keeps_what_the_player_sees(self):
        p = self.g.pending_player()
        before = json.loads(self.g.snapshot_json())
        fits = 0
        for seed in range(20):
            c = self.g.clone()
            if not c.determinize(p, seed):
                continue
            fits += 1
            after = json.loads(c.snapshot_json())
            for q in (0, 1):
                self.assertEqual(before["players"][q]["chars"], after["players"][q]["chars"])
                self.assertEqual(before["players"][q]["trash"], after["players"][q]["trash"])
                self.assertEqual(len(before["players"][q]["deck"]), len(after["players"][q]["deck"]))
            self.assertEqual(before["players"][p]["hand"], after["players"][p]["hand"])
            self.assertEqual(self.g.pending_json(), c.pending_json())
        self.assertGreater(fits, 10)


if __name__ == "__main__":
    unittest.main()
