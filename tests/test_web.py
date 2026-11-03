"""Playing in the browser, on the Rust engine. Run with: python3 -m unittest discover tests"""
import json
import random
import threading
import unittest
import urllib.error
import urllib.request

from optcg.env import rust_available
from optcg.web import Server, Session


def decks():
    from optcg.carddb import official_decks
    ds = official_decks()
    return ds["(Blue) Rocks.D.Xebec"], ds["(Yellow) Charlotte Linlin"]


def finish(s: Session, seed: int = 1) -> int:
    """Answer the person's decisions at random until the game ends; returns how many were made."""
    rng, n = random.Random(seed), 0
    while not s.g.over:
        s.act(len(s.frames) - 1, rng.randrange(len(s.frames[-1]["d"]["o"])))
        n += 1
    return n


@unittest.skipUnless(rust_available(), "optcg_rs not built")
class PlaySession(unittest.TestCase):
    def test_waits_on_the_person_and_plays_the_bot(self):
        for seat in (0, 1):
            s = Session(decks(), "greedy", human=seat, seed=2)
            d = s.frames[-1]["d"]
            self.assertEqual((d["pl"], d["pick"]), (seat, None))
            self.assertEqual(len(d["act"]), len(d["o"]))
            self.assertGreater(finish(s), 0)
            self.assertNotIn("d", s.frames[-1])

    def test_bot_hand_and_options_stay_hidden(self):
        s = Session(decks(), "greedy", human=0, seed=3)
        finish(s)
        bot = [f["d"] for f in s.frames if "d" in f and f["d"]["pl"] == 1]
        self.assertTrue(bot)
        for f in s.frames:
            self.assertTrue(all(c == {} for c in f["s"][1]["H"]))
        for d in bot:
            self.assertLessEqual(len(d["o"]), 1)
            self.assertNotIn("act", d)

    def test_stale_or_bad_answers_are_refused(self):
        s = Session(decks(), "greedy", human=0, seed=4)
        last = len(s.frames) - 1
        with self.assertRaises(ValueError):
            s.act(last, len(s.frames[-1]["d"]["o"]))
        s.act(last, 0)
        with self.assertRaises(ValueError):
            s.act(last, 0)  # already answered

    def test_cards_only_the_rust_engine_implements(self):
        from rust_parity import set_decks
        ds = set_decks("OP11")
        s = Session((ds["OP11 Jinbe"], ds["OP11 Charlotte Katakuri"]), "greedy", human=1, seed=7)
        self.assertGreater(finish(s), 0)
        self.assertNotIn("d", s.frames[-1])
        for f in s.frames:  # the bot's hand stays hidden except for cards it showed
            self.assertTrue(all(c == {} or c.get("rv") for c in f["s"][0]["H"]))

    def test_concede(self):
        s = Session(decks(), "greedy", human=1, seed=5)
        s.concede()
        self.assertEqual(s.g.winner, 0)
        self.assertNotIn("d", s.frames[-1])


@unittest.skipUnless(rust_available(), "optcg_rs not built")
class Http(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = Server(("127.0.0.1", 0), Session(decks(), "random", human=0, seed=6))
        cls.url = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.srv.server_close()

    def get(self, path):
        with urllib.request.urlopen(self.url + path) as r:
            return r.read()

    def post(self, path, body):
        req = urllib.request.Request(self.url + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_page_and_moves(self):
        self.assertIn(b'"live":{"human":0}', self.get("/"))
        st = json.loads(self.get("/api/state?from=0"))
        last = len(st["frames"]) - 1
        status, after = self.post("/api/act", {"frame": last, "option": 0})
        self.assertEqual((status, after["from"]), (200, last))
        self.assertEqual(after["frames"][0]["d"]["pick"], 0)
        status, again = self.post("/api/act", {"frame": last, "option": 0})
        self.assertEqual(status, 409)

    def test_art_stays_inside_the_assets_folder(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            self.get("/art/../optcg/web.py")
        self.assertEqual(e.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
