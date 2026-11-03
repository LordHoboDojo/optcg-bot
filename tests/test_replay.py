"""Replay recording, on the Rust engine. Run with: python3 -m unittest discover tests"""
import json
import re
import tempfile
import unittest
from pathlib import Path

from optcg.agents import GreedyAgent, RandomAgent
from optcg.env import rust_available
from optcg.play import play_game
from optcg.replay import ASSETS, art, record, to_html


def decks():
    """Official decks the Python engine also carries out in full, so both engines play the same game."""
    from optcg.carddb import official_decks
    ds = official_decks()
    return ds["(Blue) Rocks.D.Xebec"], ds["(Yellow) Charlotte Linlin"]


def embedded(html: str) -> dict:
    return json.loads(re.search(r"const R = (\{.*?\});\n", html, re.S).group(1).replace("<\\/", "</"))


@unittest.skipUnless(rust_available(), "optcg_rs not built")
class Replay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rep = record(decks(), (GreedyAgent(), RandomAgent(1)), seed=4, names=("greedy", "random"))

    def test_same_game_as_play_game(self):
        g = play_game(decks(), (GreedyAgent(), RandomAgent(1)), seed=4)
        self.assertEqual((self.rep["winner"], self.rep["turns"], self.rep["first"]),
                         (g.winner, g.turn_number, g.first_player))
        self.assertEqual(len(self.rep["frames"]), len(g.history) + 1)
        self.assertEqual([l for f in self.rep["frames"] for l in f["log"]], g.log)

    def test_frames_reference_known_cards(self):
        cards = self.rep["cards"]
        for f in self.rep["frames"]:
            for s in f["s"]:
                for c in [s["L"], s["S"], *s["C"], *s["H"]]:
                    if c:
                        self.assertIn(c["i"], cards)
                for cid in s["trash"] + [x for x in s["life"] if x]:
                    self.assertIn(cid, cards)
            if "d" in f:
                self.assertTrue(0 <= f["d"]["pick"] < len(f["d"]["o"]))
        self.assertNotIn("d", self.rep["frames"][-1])

    def test_html_embeds_the_replay(self):
        html = to_html(self.rep)
        self.assertNotIn("/*REPLAY_DATA*/null", html)
        self.assertEqual(embedded(html)["seed"], 4)
        self.assertNotIn("art", embedded(html))  # no output folder, so no relative paths to the art

    def test_no_art_without_the_assets_folder(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(art(self.rep, Path(d)), {})
            self.assertNotIn("art", embedded(to_html(self.rep, Path(d), assets=Path(d))))

    def test_cards_only_the_rust_engine_implements(self):
        """An OP15 deck: most of its text is beyond the Python engine, none of it beyond Rust."""
        from rust_parity import set_decks
        ds = set_decks("OP15")
        rep = record((ds["OP15 Enel"], ds["OP15 Brook"]), (GreedyAgent(), GreedyAgent()), seed=2)
        self.assertNotIn("d", rep["frames"][-1])
        self.assertTrue(all(not c["un"] for c in rep["cards"].values()))
        self.assertTrue(any(f["s"][0]["C"] for f in rep["frames"]))


@unittest.skipUnless(rust_available() and (ASSETS / "Cards").is_dir(), "optcg_rs not built or card art not installed")
class Art(unittest.TestCase):
    def test_paths_resolve_from_the_output_folder(self):
        from optcg.carddb import official_decks
        decks = official_decks()
        rep = record((decks["(Blue) Rocks.D.Xebec"], decks["(Yellow) Charlotte Linlin"]),
                     (GreedyAgent(), GreedyAgent()), seed=1)
        out = ASSETS.parent / "replays"
        a = embedded(to_html(rep, out))["art"]
        self.assertEqual(set(a["cards"]), set(rep["cards"]))  # every official card has art
        for path in [*a["cards"].values(), a["back"], a["don"]]:
            self.assertTrue((out / a["base"] / path).is_file(), path)


if __name__ == "__main__":
    unittest.main()
