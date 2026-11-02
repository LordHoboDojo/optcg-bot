"""Look at the top N cards and put them all back on top or all on the bottom, in any order.
Run with: python3 -m unittest discover tests"""
import unittest

from optcg.carddb import load_cards

from test_rules import battle_ready, opt

STREUSEN = "OP17-050"  # [On Play] Look at 2 cards ... place them at the top or bottom ... Then, draw 1 card.
SEARCH = "OP17-113"    # the Linlin deck's Streusen: Look at 3; reveal up to 1 {Big Mom Pirates} card


class LookAndReorder(unittest.TestCase):
    def setUp(self):
        self.g, self.p, _ = battle_ready()
        streusen = self.g.create_card(load_cards()[STREUSEN], self.p, "hand")
        self.g.refresh_options()
        self.deck = self.g.players[self.p].deck
        self.a, self.b, self.after = self.deck[-1], self.deck[-2], self.deck[-3]  # a is on top
        self.g.step(opt(self.g, "play", streusen))

    def place(self, where, first):
        g = self.g
        self.assertEqual(g.pending.kind, "select")
        self.assertIn(self.a.name, g.pending.prompt)
        g.step(next(o for o in g.pending.options if o.label.startswith(where)))
        self.assertEqual(g.pending.kind, "order_cards")  # both cards go together; only the order is asked
        g.step(next(o for o in g.pending.options if o.card == first.uid))

    def test_both_to_the_bottom_in_the_chosen_order(self):
        self.place("Bottom", first=self.b)
        self.assertEqual(self.deck[:2], [self.a, self.b])  # b chosen as the upper of the two
        self.assertIn(self.after, self.g.players[self.p].hand)  # then draw 1: the next card

    def test_both_back_on_top_in_the_chosen_order(self):
        self.place("Top", first=self.b)
        hand = self.g.players[self.p].hand
        self.assertIn(self.b, hand)                  # b was put on top, so it is the card drawn
        self.assertEqual(self.deck[-1], self.a)      # a stays just under it


class LookAndReveal(unittest.TestCase):
    """"Look at N cards; reveal up to 1 ... and add it to your hand": the opponent sees the card (11-2)."""

    def test_the_card_added_to_hand_is_named_in_the_log(self):
        g, p, _ = battle_ready()
        search = g.create_card(load_cards()[SEARCH], p, "hand")
        bmp = g.create_card(load_cards()["OP17-112"], p, "deck")  # a {Big Mom Pirates} card, now on top
        g.refresh_options()
        g.step(opt(g, "play", search))
        while bmp.zone != "hand":
            d = g.pending
            g.step(next((o for o in d.options if o.card == bmp.uid), d.options[-1]))
        self.assertIn(f"Player {p} reveals {bmp.name} and adds it to their hand", "\n".join(g.log))

        # the opponent's bot sees that one card in this hand, and nothing else from it
        from optcg.actions import Decision
        from optcg.env import CARD_FIELDS, ZONES, card_index, encode
        obs = encode(g, Decision(1 - p, "main", ()))
        col = {f: k for k, f in enumerate(CARD_FIELDS)}
        theirs = [r for r in obs["cards"] if r[col["mine"]] == 0 and r[col["zone"]] == ZONES.index("hand")]
        self.assertEqual([r[col["card"]] for r in theirs], [card_index()[bmp.d.id]])
        g.move(bmp, "trash")
        g.move(bmp, "hand")  # a card back in hand some other way is hidden again
        self.assertFalse(bmp.revealed)


if __name__ == "__main__":
    unittest.main()
