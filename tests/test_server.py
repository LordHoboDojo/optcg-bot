"""The hosted site and the bot API. Needs requirements-server.txt (skipped without it)."""
import tempfile
import unittest
from pathlib import Path

try:
    from fastapi.testclient import TestClient

    from optcg.server.app import create_app
    from optcg.server.config import Config
except ImportError:  # the server's packages aren't installed
    TestClient = None

URL = "/api/bot/v1/play"


@unittest.skipIf(TestClient is None, "needs the packages in requirements-server.txt")
class Site(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = Config(bots=["random", "greedy"], decks=["sample-red", "sample-green"], house_deck="sample-green",
                     db=":memory:", replays=Path(self.tmp.name), dev_login=True, move_timeout=5)
        self.app = create_app(cfg)
        self.c = TestClient(self.app)

    def tearDown(self):
        self.tmp.cleanup()

    def play(self, c=None):
        c = c or self.c
        r = c.post("/play", data={"deck": "sample-red", "bot_deck": "sample-green", "bot": "random"},
                   follow_redirects=False)
        self.assertEqual(r.status_code, 303)
        return c

    def key(self, name="alice-bot"):
        self.c.get("/login/dev?name=alice")
        self.c.post("/bots", data={"name": name})
        bot = self.app.state.store.bots(self.app.state.store.user("dev:alice", None, None))[-1]
        page = self.c.post(f"/bots/{bot['id']}/keys").text
        return page.split('class="key">')[1].split("<")[0]

    def connect(self, key):
        return self.c.websocket_connect(URL, headers={"Authorization": f"Bearer {key}"})

    # browser games

    def test_each_visitor_has_their_own_game(self):
        a, b = self.play(), self.play(TestClient(self.app))
        sa, sb = a.get("/api/state").json(), b.get("/api/state").json()
        a.post("/api/act", json={"frame": len(sa["frames"]) - 1, "option": 0})
        self.assertEqual(b.get("/api/state").json(), sb)
        self.assertEqual(TestClient(self.app).get("/api/state").status_code, 404)
        self.assertEqual(TestClient(self.app).get("/game", follow_redirects=False).status_code, 303)
        self.assertEqual(a.get("/game").status_code, 200)

    def test_bad_requests_get_400_and_stale_moves_409(self):
        self.play()
        self.assertEqual(self.c.get("/api/state?from=abc").status_code, 400)
        self.assertEqual(self.c.get("/api/state?from=-1").status_code, 400)
        for body in (b"{", b"[]", b'{"frame": "0", "option": 0}', b'{"frame": 0}', b'{"frame": 0, "option": -1}'):
            r = self.c.post("/api/act", content=body, headers={"content-type": "application/json"})
            self.assertEqual(r.status_code, 400, body)
        last = len(self.c.get("/api/state").json()["frames"]) - 1
        self.assertEqual(self.c.post("/api/act", json={"frame": last, "option": 0}).status_code, 200)
        self.assertEqual(self.c.post("/api/act", json={"frame": last, "option": 0}).status_code, 409)
        self.assertEqual(self.c.post("/play", data={"deck": "../etc/passwd", "bot_deck": "sample-green",
                                                    "bot": "random"}).status_code, 400)
        self.assertEqual(self.c.post("/play", data={"deck": "sample-red", "bot_deck": "sample-green",
                                                    "bot": "x.pt"}).status_code, 400)

    def test_cross_site_posts_and_big_bodies_are_refused(self):
        self.play()
        r = self.c.post("/api/concede", json={}, headers={"origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        r = self.c.post("/api/act", content=b"x" * 100_000, headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 413)

    # accounts and keys

    def test_bots_page_needs_sign_in_and_keys_can_be_revoked(self):
        self.assertEqual(self.c.get("/bots", follow_redirects=False).status_code, 303)
        key = self.key()
        self.assertIsNotNone(self.app.state.store.bot_for_key(key))
        self.assertIn("alice-bot", self.c.get("/bots").text)
        self.assertNotIn(key, self.c.get("/bots").text)  # shown once
        self.assertEqual(self.c.post("/bots", data={"name": "x"}).status_code, 400)
        kid = self.app.state.store.keys(1)[0]["id"]
        self.c.post(f"/keys/{kid}/revoke")
        self.assertIsNone(self.app.state.store.bot_for_key(key))
        self.assertIsNone(self.app.state.store.bot_for_key(key[:-1] + ("A" if key[-1] != "A" else "B")))

    def test_someone_elses_bot_gets_no_key(self):
        self.key()
        other = TestClient(self.app)
        other.get("/login/dev?name=mallory")
        self.assertNotIn('class="key"', other.post("/bots/1/keys").text)

    # the bot API

    def test_needs_a_valid_key(self):
        with self.c.websocket_connect(URL) as ws:
            self.assertEqual(ws.receive_json()["error"], "unauthorized")
        with self.connect("optcg_00000000_" + "a" * 32) as ws:
            self.assertEqual(ws.receive_json()["error"], "unauthorized")

    def test_bad_start_is_refused(self):
        key = self.key()
        for start in ({"type": "start"}, {"type": "start", "deck": "/etc/passwd"},
                      {"type": "start", "deck": "sample-red", "opponent": "runs/x.pt"},
                      {"type": "start", "deck": "sample-red", "seat": 2}):
            with self.connect(key) as ws:
                ws.send_json(start)
                self.assertEqual(ws.receive_json()["error"], "bad_request", start)

    def test_plays_a_match_to_the_end(self):
        key = self.key()
        with self.connect(key) as ws:
            ws.send_json({"type": "start", "deck": "sample-red", "opponent": "greedy", "seat": 1})
            m = ws.receive_json()
            self.assertEqual((m["type"], m["seat"], m["protocol"]), ("match", 1, 1))
            while (msg := ws.receive_json())["type"] == "decision":
                for f in msg["frames"]:  # the house bot's hand stays face down
                    self.assertTrue(all(c == {} or c.get("rv") for c in f["s"][0]["H"]))
                ws.send_json({"type": "move", "decision": msg["decision"], "option": len(msg["options"]) - 1})
        self.assertEqual((msg["type"], msg["reason"]), ("over", "normal"))
        row = self.app.state.store.match(m["match"])
        self.assertEqual((row["reason"], row["winner"]), ("normal", msg["winner"]))
        self.assertEqual(self.c.get(msg["replay"]).status_code, 200)

    def test_an_illegal_move_ends_the_match(self):
        key = self.key()
        for bad in (99, -1, "0", 1.5, True, None):
            with self.connect(key) as ws:
                ws.send_json({"type": "start", "deck": "sample-red", "seat": 0})
                m, d = ws.receive_json(), ws.receive_json()
                ws.send_json({"type": "move", "decision": d["decision"], "option": bad})
                err, over = ws.receive_json(), ws.receive_json()
            self.assertEqual(err["error"], "illegal_move", bad)
            self.assertEqual((over["type"], over["result"], over["reason"]), ("over", "loss", "illegal_move"))
            self.assertEqual(self.app.state.store.match(m["match"])["reason"], "illegal_move")

    def test_stale_and_malformed_messages_dont_end_the_match(self):
        key = self.key()
        with self.connect(key) as ws:
            ws.send_json({"type": "start", "deck": "sample-red", "seat": 0})
            ws.receive_json()
            d = ws.receive_json()
            ws.send_json({"type": "move", "decision": d["decision"] + 1, "option": 0})
            self.assertEqual(ws.receive_json(), {"type": "error", "error": "stale_decision",
                                                 "detail": f"the decision waiting is {d['decision']}",
                                                 "decision": d["decision"]})
            for junk in ("not json", "[1]", '{"type": "dance"}', '{"type": "move", "option": 0}'):
                ws.send_text(junk)
                self.assertEqual(ws.receive_json()["error"], "bad_request", junk)
            ws.send_json({"type": "move", "decision": d["decision"], "option": 0})
            self.assertIn(ws.receive_json()["type"], ("decision", "over"))
            ws.send_json({"type": "concede"})
            while (msg := ws.receive_json())["type"] != "over":
                pass
            self.assertEqual(msg["reason"], "concede")

    def test_running_out_of_time_ends_the_match(self):
        self.app.state.cfg.move_timeout = 0.2
        key = self.key()
        with self.connect(key) as ws:
            ws.send_json({"type": "start", "deck": "sample-red", "seat": 0})
            ws.receive_json(), ws.receive_json()
            self.assertEqual(ws.receive_json()["error"], "timeout")
            self.assertEqual(ws.receive_json()["reason"], "timeout")

    def test_a_bot_may_play_only_so_many_matches_at_once(self):
        self.app.state.cfg.bot_matches = 1
        key = self.key()
        with self.connect(key) as ws:
            ws.send_json({"type": "start", "deck": "sample-red", "seat": 0})
            ws.receive_json(), ws.receive_json()
            with self.connect(key) as ws2:
                self.assertEqual(ws2.receive_json()["error"], "too_many_matches")


if __name__ == "__main__":
    unittest.main()
