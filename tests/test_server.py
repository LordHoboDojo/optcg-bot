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
        cfg = Config(bots=["random", "greedy"], decks=["rocks", "(Yellow) Charlotte Linlin"], house_deck="(Yellow) Charlotte Linlin",
                     db=":memory:", replays=Path(self.tmp.name), dev_login=True, move_timeout=5)
        self.app = create_app(cfg)
        self.c = TestClient(self.app)

    def tearDown(self):
        self.tmp.cleanup()

    def play(self, c=None, bot="random"):
        c = c or self.c
        r = c.post("/api/games", json={"deck": "rocks", "bot_deck": "(Yellow) Charlotte Linlin", "bot": bot})
        self.assertEqual(r.status_code, 200, r.text)
        return c

    def login(self, c=None, name="alice"):
        c = c or self.c
        c.get(f"/login/dev?name={name}", follow_redirects=False)
        return c

    def key(self, name="alice-bot"):
        self.login()
        bot_id = self.c.post("/api/bots", json={"name": name}).json()["id"]
        return self.c.post(f"/api/bots/{bot_id}/keys").json()["key"]

    def connect(self, key):
        return self.c.websocket_connect(URL, headers={"Authorization": f"Bearer {key}"})

    # browser games

    def test_each_visitor_has_their_own_game(self):
        a, b = self.play(), self.play(TestClient(self.app))
        sa, sb = a.get("/api/state").json(), b.get("/api/state").json()
        a.post("/api/act", json={"frame": len(sa["frames"]) - 1, "option": 0})
        self.assertEqual(b.get("/api/state").json(), sb)
        self.assertEqual(TestClient(self.app).get("/api/state").status_code, 404)
        self.assertEqual(TestClient(self.app).get("/game", follow_redirects=False).headers["location"], "/play")
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
        for bad in ({"deck": "../etc/passwd", "bot_deck": "(Yellow) Charlotte Linlin", "bot": "random"},
                    {"deck": "rocks", "bot_deck": "(Yellow) Charlotte Linlin", "bot": "x.pt"},
                    {"deck": "rocks", "bot_deck": "(Yellow) Charlotte Linlin", "bot": "random", "seat": 2}):
            self.assertEqual(self.c.post("/api/games", json=bad).status_code, 400, bad)

    def test_cross_site_posts_and_big_bodies_are_refused(self):
        self.play()
        r = self.c.post("/api/concede", json={}, headers={"origin": "https://evil.example"})
        self.assertEqual(r.status_code, 403)
        r = self.c.post("/api/act", content=b"x" * 100_000, headers={"content-type": "application/json"})
        self.assertEqual(r.status_code, 413)

    # accounts and keys

    def test_bots_need_sign_in_and_keys_can_be_revoked(self):
        self.assertEqual(self.c.get("/api/bots").status_code, 401)
        self.assertEqual(self.c.post("/api/bots", json={"name": "abc"}).status_code, 401)
        key = self.key()
        self.assertIsNotNone(self.app.state.store.bot_for_key(key))
        listed = self.c.get("/api/bots").json()
        self.assertEqual(listed["bots"][0]["name"], "alice-bot")
        self.assertNotIn(key, str(listed))  # shown once
        self.assertEqual(self.c.post("/api/bots", json={"name": "x"}).status_code, 400)
        self.assertEqual(self.c.post("/api/bots", json={"name": "alice-bot"}).status_code, 400)  # taken
        kid = listed["bots"][0]["keys"][0]["id"]
        self.assertEqual(self.c.delete(f"/api/keys/{kid}").status_code, 200)
        self.assertEqual(self.c.delete(f"/api/keys/{kid}").status_code, 404)
        self.assertIsNone(self.app.state.store.bot_for_key(key))
        self.assertIsNone(self.app.state.store.bot_for_key(key[:-1] + ("A" if key[-1] != "A" else "B")))

    def test_retiring_a_bot_stops_its_keys_and_frees_its_name(self):
        key = self.key()
        bot_id = self.c.get("/api/bots").json()["bots"][0]["id"]
        self.assertEqual(self.c.delete(f"/api/bots/{bot_id}").status_code, 200)
        self.assertIsNone(self.app.state.store.bot_for_key(key))
        self.assertEqual(self.c.get("/api/bots").json()["bots"], [])
        self.assertEqual(self.c.post("/api/bots", json={"name": "alice-bot"}).status_code, 200)

    def test_someone_elses_bot_gets_no_key(self):
        self.key()
        other = self.login(TestClient(self.app), "mallory")
        self.assertEqual(other.post("/api/bots/1/keys").status_code, 404)
        self.assertEqual(other.delete("/api/bots/1").status_code, 404)
        self.assertEqual(other.delete("/api/keys/1").status_code, 404)

    def test_account_records_games_and_can_be_renamed_and_deleted(self):
        self.assertEqual(self.c.get("/api/account").status_code, 401)
        self.login()
        self.play()
        self.c.post("/api/concede", json={})
        acc = self.c.get("/api/account").json()
        self.assertEqual(acc["totals"], {"played": 1, "won": 0})
        self.assertEqual((acc["games"][0]["result"], acc["games"][0]["reason"]), ("loss", "concede"))
        self.c.post("/api/concede", json={})  # already over: not counted twice
        self.assertEqual(self.c.get("/api/account").json()["totals"]["played"], 1)
        self.assertEqual(self.c.patch("/api/account", json={"name": "Al"}).status_code, 200)
        self.assertEqual(self.c.get("/api/me").json()["user"]["name"], "Al")
        self.assertEqual(self.c.patch("/api/account", json={"name": " "}).status_code, 400)
        self.assertEqual(self.app.state.store.house_record()["random"]["people"], {"played": 1, "won": 1})
        self.assertEqual(self.c.delete("/api/account").status_code, 200)
        self.assertIsNone(self.c.get("/api/me").json()["user"])
        self.assertIsNone(self.app.state.store.get_user(1))

    def test_options_and_pages(self):
        o = self.c.get("/api/options").json()
        self.assertEqual([d["name"] for d in o["decks"]], ["rocks", "(Yellow) Charlotte Linlin"])
        self.assertTrue(all(0 <= d["coverage"] <= 1 and d["leader_id"] for d in o["decks"]))
        self.assertEqual([b["name"] for b in o["bots"]], ["random", "greedy"])
        self.assertIn("analysis", self.c.get("/api/analysis").json())
        self.assertEqual(self.c.get("/api/nope").status_code, 404)
        self.assertIn(self.c.get("/play").status_code, (200, 503))  # 503 when web/dist isn't built
        self.assertIn(self.c.get("/no-such-page").status_code, (404, 503))

    # the bot API

    def test_needs_a_valid_key(self):
        with self.c.websocket_connect(URL) as ws:
            self.assertEqual(ws.receive_json()["error"], "unauthorized")
        with self.connect("optcg_00000000_" + "a" * 32) as ws:
            self.assertEqual(ws.receive_json()["error"], "unauthorized")

    def test_bad_start_is_refused(self):
        key = self.key()
        for start in ({"type": "start"}, {"type": "start", "deck": "/etc/passwd"},
                      {"type": "start", "deck": "rocks", "opponent": "runs/x.pt"},
                      {"type": "start", "deck": "rocks", "seat": 2}):
            with self.connect(key) as ws:
                ws.send_json(start)
                self.assertEqual(ws.receive_json()["error"], "bad_request", start)

    def test_plays_a_match_to_the_end(self):
        key = self.key()
        with self.connect(key) as ws:
            ws.send_json({"type": "start", "deck": "rocks", "opponent": "greedy", "seat": 1})
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
                ws.send_json({"type": "start", "deck": "rocks", "seat": 0})
                m, d = ws.receive_json(), ws.receive_json()
                ws.send_json({"type": "move", "decision": d["decision"], "option": bad})
                err, over = ws.receive_json(), ws.receive_json()
            self.assertEqual(err["error"], "illegal_move", bad)
            self.assertEqual((over["type"], over["result"], over["reason"]), ("over", "loss", "illegal_move"))
            self.assertEqual(self.app.state.store.match(m["match"])["reason"], "illegal_move")

    def test_stale_and_malformed_messages_dont_end_the_match(self):
        key = self.key()
        with self.connect(key) as ws:
            ws.send_json({"type": "start", "deck": "rocks", "seat": 0})
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
            ws.send_json({"type": "start", "deck": "rocks", "seat": 0})
            ws.receive_json(), ws.receive_json()
            self.assertEqual(ws.receive_json()["error"], "timeout")
            self.assertEqual(ws.receive_json()["reason"], "timeout")

    def test_a_bot_may_play_only_so_many_matches_at_once(self):
        self.app.state.cfg.bot_matches = 1
        key = self.key()
        with self.connect(key) as ws:
            ws.send_json({"type": "start", "deck": "rocks", "seat": 0})
            ws.receive_json(), ws.receive_json()
            with self.connect(key) as ws2:
                self.assertEqual(ws2.receive_json()["error"], "too_many_matches")


if __name__ == "__main__":
    unittest.main()
