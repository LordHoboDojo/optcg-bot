"""Replay recording. Run with: python3 -m unittest discover tests"""
import json
import re
import tempfile
import unittest
from pathlib import Path

from optcg.agents import GreedyAgent, RandomAgent
from optcg.cardpool import GREEN_DECK, RED_DECK
from optcg.play import play_game
from optcg.replay import ASSETS, art, record, to_html


def embedded(html: str) -> dict:
    return json.loads(re.search(r"const R = (\{.*?\});\n", html, re.S).group(1).replace("<\\/", "</"))


class Replay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rep = record((RED_DECK, GREEN_DECK), (GreedyAgent(), RandomAgent(1)), seed=4, names=("greedy", "random"))

    def test_same_game_as_play_game(self):
        g = play_game((RED_DECK, GREEN_DECK), (GreedyAgent(), RandomAgent(1)), seed=4)
        self.assertEqual((self.rep["winner"], self.rep["turns"]), (g.winner, g.turn_number))
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


@unittest.skipUnless((ASSETS / "Cards").is_dir(), "card art not installed")
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
