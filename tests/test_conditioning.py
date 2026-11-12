"""Deck conditioning: what observations say about the decks (only the player's own list and the
opponent's Leader), the per-card features, and how a conditioned network reads them. The network
tests need PyTorch; the Rust ones need the Rust engine (engine-rs/)."""
import unittest

from optcg.agents import GreedyAgent
from optcg.engine import Game
from optcg.env import Env, card_index, encode, encode_rust, rust_available, rust_game
from optcg.play import resolve_deck

try:
    import torch
except ImportError:
    torch = None

ROCKS, LINLIN, LUFFY = "(Blue) Rocks.D.Xebec", "(Yellow) Charlotte Linlin", "(Black) Monkey.D.Luffy"


def _ids(deck):
    ids = card_index()
    return [ids[deck.leader.id]] + sorted(ids[c.id] for c in deck.cards)


class DeckObservation(unittest.TestCase):
    def setUp(self):
        self.decks = (resolve_deck(ROCKS), resolve_deck(LINLIN))

    def check(self, obs):
        me = obs["player"]
        mine, theirs = self.decks[me], self.decks[1 - me]
        self.assertEqual([obs["deck"][0]] + sorted(obs["deck"][1:]), _ids(mine))
        self.assertEqual(obs["opp_leader"], card_index()[theirs.leader.id])
        # nothing of the opponent's list but its Leader: no card only they run shows up
        only_theirs = {card_index()[c.id] for c in theirs.cards} - {card_index()[c.id] for c in mine.cards}
        self.assertFalse(only_theirs & set(obs["deck"]))

    def test_python_engine(self):
        g = Game(self.decks, seed=3)
        seen = set()
        for _ in range(200):
            if g.over:
                break
            self.check(encode(g, g.pending))
            seen.add(g.pending.player)
            g.step(GreedyAgent().act(g, g.pending))
        self.assertEqual(seen, {0, 1})

    @unittest.skipUnless(rust_available(), "Rust engine not built")
    def test_rust_engine_matches(self):
        g = rust_game(self.decks, 3)
        py = Game(self.decks, seed=3)
        for _ in range(40):
            a, b = encode_rust(g), encode(py, py.pending)
            self.check(a)
            self.assertEqual(a["deck"], b["deck"])
            self.assertEqual(a["opp_leader"], b["opp_leader"])
            k = g.greedy()
            g.step(k)
            py.step(py.pending.options[k])

    @unittest.skipUnless(rust_available(), "Rust engine not built")
    def test_opponents_list_is_hidden(self):
        """Two games that differ only in the cards behind the opponent's Leader look the same to
        the player at the first decision."""
        other = resolve_deck(LINLIN)
        swap = resolve_deck(LUFFY)
        other.cards = list(swap.cards)  # Linlin's Leader, Luffy's cards
        a = encode_rust(rust_game((self.decks[0], self.decks[1]), 5))
        b = encode_rust(rust_game((self.decks[0], other), 5))
        if a["player"] == 0 and b["player"] == 0:
            self.assertEqual(a, b)

    def test_env_observations_carry_it(self):
        env = Env(*self.decks, opponent=GreedyAgent(), seat=1)
        obs = env.reset(0)
        self.assertEqual(obs["player"], 1)
        self.check(obs)


@unittest.skipIf(torch is None, "PyTorch not installed")
class CardFeatures(unittest.TestCase):
    def test_table(self):
        from optcg.nn.model import card_feature_names, card_features
        f, names = card_features(), card_feature_names()
        self.assertEqual(f.shape, (len(card_index()) + 1, len(names)))
        self.assertTrue((f[0] == 0).all())  # no card
        self.assertTrue(((f >= 0) & (f <= 1.5)).all())
        rocks = resolve_deck(ROCKS)
        row = f[card_index()[rocks.leader.id]]
        self.assertEqual(row[names.index("Leader")], 1)
        self.assertEqual(row[names.index("blue")], 1)
        self.assertAlmostEqual(float(row[names.index("life")]), 1.0)

    @unittest.skipUnless(rust_available(), "Rust engine not built")
    def test_effect_tokens(self):
        """Cards share columns by what their compiled effects do."""
        from optcg.nn.model import card_feature_names, card_features
        f, names = card_features(), card_feature_names()
        for token in ("kw:Blocker", "t:OnPlay", "t:Trigger", "a:draw", "a:ko", "cost:don_minus"):
            self.assertIn(token, names)
        from optcg.env import rust_db
        ids = card_index()
        rocks = resolve_deck(ROCKS)
        leader = f[ids[rocks.leader.id]]  # [When Attacking] ... (a hand-written script: "script")
        self.assertGreater(leader[names.index("t:WhenAttacking")], 0)
        self.assertGreater(leader[names.index("script")], 0)
        drawn = 0
        for c in rocks.cards:  # every token a card's effects compile to lights its column
            for t in rust_db().card_tokens(c.id):
                col = names.index(t) if t in names else names.index(t.split(":")[0] + ":other")
                self.assertGreater(f[ids[c.id], col], 0, f"{c.id} {t}")
                drawn += t == "a:draw"
        self.assertGreater(drawn, 0)
        blockers = [c for c in rocks.cards if "Blocker" in c.keywords]
        if blockers:
            self.assertGreater(f[ids[blockers[0].id], names.index("kw:Blocker")], 0)


@unittest.skipIf(torch is None, "PyTorch not installed")
class ConditionedNet(unittest.TestCase):
    def setUp(self):
        from optcg.nn.model import conditioned
        torch.manual_seed(0)
        self.net = conditioned(32).eval()
        env = Env(resolve_deck(ROCKS), resolve_deck(LINLIN), GreedyAgent())
        obs, self.obs = env.reset(0), []
        while not env.done and len(self.obs) < 12:
            self.obs.append(obs)
            obs, _, _ = env.step(0)

    def score(self, obs):
        from optcg.nn.model import collate
        with torch.no_grad():
            return self.net(collate(obs))

    def test_reads_the_decklist(self):
        o = self.obs[0]
        luffy = _ids(resolve_deck(LUFFY))
        a, va = self.score([o])
        b, vb = self.score([dict(o, deck=luffy)])
        c, vc = self.score([dict(o, opp_leader=luffy[0])])
        self.assertFalse(torch.allclose(va, vb))
        self.assertFalse(torch.allclose(va, vc))

    def test_card_order_doesnt_matter(self):
        o = self.obs[0]
        shuffled = [o["deck"][0]] + list(reversed(o["deck"][1:]))
        (a, va), (b, vb) = self.score([o]), self.score([dict(o, deck=shuffled)])
        self.assertTrue(torch.allclose(va, vb, atol=1e-5))
        self.assertTrue(torch.allclose(a, b, atol=1e-5))

    def test_batch_matches_single_with_padding(self):
        obs = self.obs + [dict(self.obs[0], deck=self.obs[0]["deck"][:30])]  # a shorter list pads
        batch, _ = self.score(obs)
        for i in (0, len(obs) - 1):
            one, _ = self.score([obs[i]])
            n = len(obs[i]["options"])
            self.assertTrue(torch.allclose(batch[i, :n], one[0, :n], atol=1e-5))

    def test_needs_the_decklist(self):
        from optcg.nn.model import collate
        o = {k: v for k, v in self.obs[0].items() if k not in ("deck", "opp_leader")}
        with self.assertRaises(ValueError):
            self.net(collate([o]))

    def test_save_load_keeps_features_and_old_checkpoints_load(self):
        import tempfile
        from pathlib import Path

        from optcg.nn.model import PolicyNet, load, save
        with tempfile.TemporaryDirectory() as d:
            p = str(Path(d) / "c.pt")
            save(self.net, p)
            back = load(p)
            self.assertEqual(back.config(), self.net.config())
            self.assertTrue(torch.equal(back.card_feat, self.net.card_feat))
            before = self.score(self.obs)[0]
            self.net = back
            self.assertTrue(torch.equal(before, self.score(self.obs)[0]))
            # a checkpoint from before conditioning: no config, the original network
            old = PolicyNet(32)
            torch.save({"state": old.state_dict(), "hidden": 32, "cards": card_index()}, p)
            self.assertFalse(load(p).deck_cond)

    def test_hidden_256(self):
        from optcg.nn.model import collate, conditioned
        net = conditioned(256).eval()
        with torch.no_grad():
            logits, value = net(collate(self.obs))
        self.assertEqual(value.shape, (len(self.obs),))


if __name__ == "__main__":
    unittest.main()
