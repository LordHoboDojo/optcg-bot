"""Tests for real card data and the effect-text compiler. Run with: python3 -m unittest discover tests"""
import unittest

from optcg import Action, Deck, DeckError
from optcg.agents import GreedyAgent, RandomAgent
from optcg.carddb import DATA, coverage, load_cards, official_decks, parse_decklist
from optcg.cards import BLOCKER, RUSH, Timing
from optcg.effect_text import compile_card
from optcg.play import play_game

from test_rules import add_life, has_opt, opt, to_turn, wipe

HAVE_DATA = (DATA / "cards.json").exists() and (DATA / "decks.json").exists()


class Compiler(unittest.TestCase):
    def test_keywords_and_explanations(self):
        c = compile_card("[Blocker] (After your opponent declares an attack, you may rest this card.)")
        self.assertEqual(c.keywords, {BLOCKER})
        self.assertEqual(c.unimplemented, [])

    def test_timing_cost_and_conditions(self):
        c = compile_card("[DON!! x1] [When Attacking] [Once Per Turn] ➀ (You may rest the specified number of "
                         "DON!! cards in your cost area.): Draw 1 card.")
        e, = c.effects
        self.assertEqual((e.timing, e.don_req, e.once_per_turn, e.rest_don, e.optional),
                         (Timing.WHEN_ATTACKING, 1, True, 1, True))

    def test_multi_sentence_and_trigger(self):
        c = compile_card("[On Play] K.O. up to 1 of your opponent's Characters with a cost of 3 or less. "
                         "Then, draw 1 card.", "[Trigger] Activate this card's [On Play] effect.")
        self.assertEqual([e.timing for e in c.effects], [Timing.ON_PLAY, Timing.TRIGGER])

    def test_static_keyword(self):
        c = compile_card("[DON!! x1] This Character gains [Rush].")
        self.assertEqual(c.effects, [])
        self.assertEqual(len(c.static_keywords), 1)

    def test_choose_one(self):
        c = compile_card("[Main] Choose one:\n• Draw 1 card.\n• K.O. up to 1 of your opponent's Characters "
                         "with a cost of 2 or less.")
        self.assertEqual((len(c.effects), c.unimplemented), (1, []))

    def test_unknown_text_is_reported_not_guessed(self):
        text = "[On Play] Swap your Leader with a Character in your trash."
        c = compile_card(text)
        self.assertEqual((c.effects, c.unimplemented), ([], [text]))


@unittest.skipUnless(HAVE_DATA, "run python3 tools/fetch_cards.py first")
class RealCards(unittest.TestCase):
    def test_known_card(self):
        luffy = load_cards()["ST01-012"]
        self.assertEqual((luffy.cost, luffy.power, luffy.colors), (5, 6000, ("red",)))
        self.assertIn(RUSH, luffy.keywords)
        self.assertEqual(luffy.unimplemented, ())

    def test_official_decks_load_and_validate(self):
        decks = official_decks()
        self.assertGreater(len(decks), 100)
        for deck in decks.values():
            deck.validate()

    def test_decklist_parsing(self):
        cards = load_cards()
        zoro = official_decks()["(Red) Roronoa Zoro"]
        lines = [f"1x{zoro.leader.id}"] + [f"{zoro.cards.count(d)}x{d.id}" for d in dict.fromkeys(zoro.cards)]
        deck = parse_decklist("\n".join(lines), cards)
        self.assertEqual(sorted(d.id for d in deck.cards), sorted(d.id for d in zoro.cards))
        with self.assertRaises(DeckError):
            parse_decklist("4xOP01-016", cards)  # no Leader

    def test_coverage_counts(self):
        done, total, missing = coverage(official_decks()["(Red) Roronoa Zoro"])
        self.assertLessEqual(done, total)
        self.assertEqual(total - done, len(missing))

    def test_search_effect_in_game(self):
        """OP01-016 Nami: [On Play] look at 5, add a {Straw Hat Crew} card other than [Nami]."""
        from test_rules import new_game
        cards = load_cards()
        g = new_game()
        to_turn(g, 3)
        wipe(g)
        p = g.turn_player
        add_life(g, 1 - p)
        nami = g.create_card(cards["OP01-016"], p, "hand")
        for _ in range(5):
            g.create_card(cards["ST01-012"], p, "deck")  # Luffy, {Straw Hat Crew}
        g.refresh_options()
        g.step(opt(g, "play", card=nami))
        g.step(opt(g, "select"))
        self.assertEqual([c.d.id for c in g.players[p].hand], ["ST01-012"])

    def test_real_deck_games_finish(self):
        decks = list(official_decks().values())
        for i in range(60):
            pair = (decks[i % len(decks)], decks[(i * 7 + 3) % len(decks)])
            agents = (GreedyAgent(), RandomAgent(i)) if i % 2 else (RandomAgent(i), GreedyAgent())
            g = play_game(pair, agents, seed=i)
            self.assertTrue(g.over)
            for ps in g.players:
                self.assertEqual(ps.don_on_field + ps.don_deck, 10)


@unittest.skipUnless(HAVE_DATA, "run python3 tools/fetch_cards.py first")
class OP17Leaders(unittest.TestCase):
    def setUp(self):
        from test_rules import battle_ready
        self.cards = load_cards()
        self.g, self.p, self.o = battle_ready()

    def lead(self, card_id):
        leader = self.g.create_card(self.cards[card_id], self.p, "leader")
        self.g.refresh_options()
        return leader

    def test_all_leaders_implemented(self):
        leaders = [d for i, d in self.cards.items() if i.startswith("OP17-") and d.category.value == "Leader"]
        self.assertEqual(len(leaders), 6)
        self.assertEqual([d.id for d in leaders if d.unimplemented], [])

    def test_shanks_keeps_character_rested(self):
        g, p, o = self.g, self.p, self.o
        leader = self.lead("OP17-020")
        target = g.create_card(self.cards["OP17-008"], o, "character")
        target.rested = True
        g.refresh_options()
        g.step(opt(g, "activate", card=leader))  # hand is empty, so the cost is resting 1 DON!!
        g.step(opt(g, "select", card=target))
        to_turn(g, 4)
        self.assertTrue(target.rested)  # skipped this Refresh Phase
        to_turn(g, 6)
        self.assertFalse(target.rested)

    def test_kaido_alternative_timings_share_once_per_turn(self):
        g, p, o = self.g, self.p, self.o
        leader = self.lead("OP17-058")
        victim = g.create_card(self.cards["OP17-008"], o, "character")
        g.step(opt(g, "attack", card=leader, target=g.players[o].leader))
        g.step(opt(g, "yes"))
        g.step(opt(g, "select", card=victim))
        self.assertEqual(g.power(victim), 6000)
        self.assertEqual(g.players[p].don_deck, 8)  # DON!! -1 paid
        self.assertFalse(g.can_activate(leader, 1))  # the [On Your Opponent's Attack] half is used up too

    def test_luffy_gives_blocker_to_cost_12_characters(self):
        g, p = self.g, self.p
        self.lead("OP17-079")
        dorry = g.create_card(self.cards["OP17-085"], p, "character")  # This Character gains +12 cost.
        jozu = g.create_card(self.cards["OP17-008"], p, "character")
        self.assertEqual(g.cost(dorry), 17)
        self.assertTrue(g.has_keyword(dorry, BLOCKER))
        self.assertFalse(g.has_keyword(jozu, BLOCKER))

    def test_linlin_opponent_chooses(self):
        g, p, o = self.g, self.p, self.o
        leader = self.lead("OP17-099")
        g.create_card(self.cards["OP17-008"], p, "hand")
        g.refresh_options()
        g.step(opt(g, "attack", card=leader, target=g.players[o].leader))
        g.step(opt(g, "yes"))  # pay: trash 1 card
        self.assertEqual((g.pending.kind, g.pending.player), ("choose_one", o))

    def test_jozu_base_power_lasts_through_opponents_turn(self):
        g, p = self.g, self.p
        leader = self.lead("OP17-001")
        jozu = g.create_card(self.cards["OP17-008"], p, "hand")
        g.players[p].don_active = 10
        g.refresh_options()
        g.step(opt(g, "play", card=jozu))
        self.assertEqual(g.power(leader), 8000)
        to_turn(g, 4)
        self.assertEqual(g.power(leader), 8000)
        to_turn(g, 5)
        self.assertEqual(g.power(leader), 5000)


def run_effect(gen, pick=lambda d: Action("yes") if d.kind == "confirm" else d.options[0]):
    """Drive one effect generator outside the game loop, answering each decision with `pick`."""
    try:
        d = next(gen)
        while True:
            d = gen.send(pick(d))
    except StopIteration as e:
        return e.value


@unittest.skipUnless(HAVE_DATA, "run python3 tools/fetch_cards.py first")
class OP17Mechanics(unittest.TestCase):
    def setUp(self):
        from test_rules import battle_ready
        self.cards = load_cards()
        self.g, self.p, self.o = battle_ready()

    def make(self, card_id, player, zone="character"):
        return self.g.create_card(self.cards[card_id], player, zone)

    def test_all_op17_effects_implemented(self):
        missing = [(i, d.unimplemented) for i, d in self.cards.items() if i.startswith("OP17-") and d.unimplemented]
        self.assertEqual(missing, [])

    def test_kyo_replaces_removal_by_opponent(self):
        g, p, o = self.g, self.p, self.o
        self.make("OP17-045", o)
        victim = self.make("OP17-008", o)
        for _ in range(2):
            self.make("OP17-008", o, "hand")
        self.assertFalse(run_effect(g.remove_from_field(victim, "hand", by=p)))
        self.assertIn(victim, g.players[o].characters)
        self.assertEqual(len(g.players[o].hand), 0)
        self.assertTrue(run_effect(g.remove_from_field(victim, "hand", by=p)))  # can't pay twice

    def test_kyo_ignores_own_effects(self):
        g, o = self.g, self.o
        self.make("OP17-045", o)
        victim = self.make("OP17-008", o)
        self.make("OP17-008", o, "hand")
        self.make("OP17-008", o, "hand")
        self.assertTrue(run_effect(g.remove_from_field(victim, "hand", by=o)))

    def test_marco_is_ko_d_instead(self):
        g, p, o = self.g, self.p, self.o
        marco = self.make("OP17-015", o)
        victim = self.make("OP17-008", o)
        run_effect(g.ko(victim, by=p))
        self.assertIn(victim, g.players[o].characters)
        self.assertIn(marco, g.players[o].trash)

    def test_ganzui_survives_battle(self):
        g, p, o = self.g, self.p, self.o
        ganzui = self.make("OP17-043", o)
        ganzui.rested = True
        self.make("OP17-008", o, "hand")
        self.make("OP17-008", o, "hand")
        leader = g.players[p].leader
        g.players[p].don_active = 10
        g.refresh_options()
        for _ in range(5):
            g.step(opt(g, "give_don", target=leader))
        g.step(opt(g, "attack", card=leader, target=ganzui))
        while g.pending.player == o:  # no counters; use the replacement and pay it
            d = g.pending
            g.step(opt(g, "yes") if d.kind == "confirm" else opt(g, "pass") if d.kind == "counter" else d.options[0])
        self.assertIn(ganzui, g.players[o].characters)

    def test_captain_john_forces_attacks(self):
        g, p, o = self.g, self.p, self.o
        g.create_card(self.cards["OP17-039"], o, "leader")  # Rocks Pirates Leader
        john = self.make("OP17-044", o)
        other = self.make("OP17-008", o)
        john.rested = other.rested = True
        g.refresh_options()
        targets = {a.target for a in g.pending.options if a.kind == "attack"}
        self.assertEqual(targets, {john.uid})

    def test_hand_cost_and_counters(self):
        g, p, o = self.g, self.p, self.o
        newgate = self.make("OP17-005", p, "hand")
        self.assertEqual(g.cost(newgate), 10)
        self.make("OP17-008", o).mods.append(__import__("optcg.cards").cards.Mod("power", 2000, "turn", o))
        self.assertEqual(g.cost(newgate), 6)  # opponent has a 10000-power Character

        no_counter = self.make("OP17-008", p, "hand")
        self.assertEqual(g.counter_value(no_counter), 0)
        self.make("OP17-063", p)  # Kaido: Characters in hand without a Counter have +1000
        self.assertEqual(g.counter_value(no_counter), 1000)
        xebec = self.make("OP17-118", p, "hand")
        self.assertEqual(g.counter_value(xebec), 2000)  # your only Character (Kaido) has no Counter

    def test_kaido_negates_and_don_return_triggers(self):
        g, p, o = self.g, self.p, self.o
        self.make("OP17-062", p)
        kaido = self.make("OP17-063", p)
        kaido.played_turn = g.turn_number
        from optcg.cardpool import G_TREANT
        treant = g.create_card(G_TREANT, o, "character")  # cost 6, [On K.O.] Draw 2
        ps = g.players[p]
        before = (ps.don_deck, ps.don_active)
        g.refresh_options()
        g.step(opt(g, "activate", card=kaido))
        g.step(opt(g, "select", card=treant))
        self.assertIn(treant, g.players[o].trash)
        self.assertEqual(len(g.players[o].hand), 0)  # negated: no [On K.O.] draw
        # DON!! -1 returned one; the other Kaido added it back as active.
        self.assertEqual((ps.don_deck, ps.don_active), before)

    def test_dorry_stops_character_plays(self):
        g, p = self.g, self.p
        g.create_card(self.cards["OP17-079"], p, "leader")  # {Elbaph} Leader
        dorry = self.make("OP17-085", p, "hand")
        other = self.make("OP17-008", p, "hand")
        g.players[p].don_active = 10
        g.refresh_options()
        g.step(opt(g, "play", card=dorry))
        self.assertFalse(has_opt(g, "play", card=other))

    def test_linlin_sets_trigger_characters_to_8000(self):
        g, p = self.g, self.p
        trigger_4000 = next(d for d in self.cards.values() if d.category.value == "Character"
                            and d.power == 4000 and d.trigger_text)
        c = g.create_card(trigger_4000, p, "character")
        self.make("OP17-112", p)
        self.assertEqual(g.power(c), 8000)
        to_turn(g, 4)
        self.assertEqual(g.power(c), 4000)  # [Your Turn] only


if __name__ == "__main__":
    unittest.main()
