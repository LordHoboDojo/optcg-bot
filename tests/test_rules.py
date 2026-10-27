"""Rule tests. Run with: python3 -m unittest discover tests"""
import unittest

from optcg import Action, CardDef, Category, Deck, DeckError, Game
from optcg.agents import GreedyAgent, RandomAgent
from optcg.cardpool import (G_TREANT, GREEN_DECK, GREEN_LEADER, R_BRAWLER, R_BRUISER, R_DECKHAND,
                            R_FIRE_FIST, R_LOOKOUT, R_ROOKIE, RED_DECK, RED_LEADER, G_THORN)
from optcg.play import play_game


def new_game(seed=0) -> Game:
    g = Game((RED_DECK, GREEN_DECK), seed=seed)
    while g.pending.kind in ("go_first", "mulligan"):
        if g.pending.kind == "go_first":  # player 0 (red) always goes first
            g.step(Action("first" if g.pending.player == 0 else "second"))
        else:
            g.step(Action("keep"))
    return g


def to_turn(g: Game, turn: int) -> None:
    """End turns until the main phase of `turn`."""
    while not (g.turn_number == turn and g.pending.kind == "main"):
        g.step(Action("end") if g.pending.kind == "main" else g.pending.options[0])


def wipe(g: Game) -> None:
    """Empty both hands, Character areas, Stages and Life (decks stay)."""
    for ps in g.players:
        for c in [*ps.hand, *ps.characters, *ps.life] + ([ps.stage] if ps.stage else []):
            g.move(c, "trash")


def add_life(g: Game, player: int, n: int = 5, card=R_BRUISER) -> None:
    for _ in range(n):
        g.create_card(card, player, "life")


def opt(g: Game, kind: str, card=None, target=None) -> Action:
    for o in g.pending.options:
        if o.kind == kind and (card is None or o.card == card.uid) and (target is None or o.target == target.uid):
            return o
    raise AssertionError(f"no {kind} option; have {[str(o) for o in g.pending.options]}")


def has_opt(g: Game, kind: str, card=None, target=None) -> bool:
    try:
        opt(g, kind, card, target)
        return True
    except AssertionError:
        return False


def battle_ready(seed=0):
    """Turn 3 (first player's second turn), empty boards, 5 vanilla Life each."""
    g = new_game(seed)
    to_turn(g, 3)
    wipe(g)
    p = g.turn_player
    add_life(g, p)
    add_life(g, 1 - p)
    g.refresh_options()
    return g, p, 1 - p


class SetupAndTurns(unittest.TestCase):
    def test_opening_state(self):
        g = new_game()
        p, o = g.turn_player, 1 - g.turn_player
        self.assertEqual(g.turn_number, 1)
        for q in (p, o):
            ps = g.players[q]
            self.assertEqual((len(ps.hand), len(ps.life), len(ps.deck)), (5, 5, 40))
        self.assertEqual(g.players[p].don_active, 1)  # first player gets 1 DON!! on turn 1, no draw
        self.assertEqual(g.players[o].don_active, 0)

    def test_second_player_draws_and_gets_two_don(self):
        g = new_game()
        to_turn(g, 2)
        ps = g.players[g.turn_player]
        self.assertEqual((len(ps.hand), ps.don_active), (6, 2))

    def test_no_battles_on_first_turns(self):
        g = new_game()
        for turn in (1, 2):
            to_turn(g, turn)
            self.assertFalse(has_opt(g, "attack"))
        to_turn(g, 3)
        self.assertTrue(has_opt(g, "attack"))

    def test_don_power_only_on_own_turn_and_returns_on_refresh(self):
        g, p, o = battle_ready()
        leader = g.players[p].leader
        for _ in range(2):
            g.step(opt(g, "give_don", target=leader))
        self.assertEqual(g.power(leader), 7000)
        to_turn(g, 4)
        self.assertEqual(leader.don, 2)
        self.assertEqual(g.power(leader), 5000)
        to_turn(g, 5)
        self.assertEqual(leader.don, 0)
        self.assertEqual(g.players[p].don_active, 5)  # 1 + 2 + 2, the given ones back and active

    def test_character_limit_forces_trash(self):
        g, p, o = battle_ready()
        for _ in range(5):
            g.create_card(R_DECKHAND, p, "character")
        new = g.create_card(R_DECKHAND, p, "hand")
        g.refresh_options()
        g.step(opt(g, "play", card=new))
        self.assertEqual(g.pending.kind, "select")
        self.assertFalse(has_opt(g, "pass"))  # trashing is mandatory
        g.step(g.pending.options[0])
        self.assertEqual(len(g.players[p].characters), 5)
        self.assertIn(new, g.players[p].characters)

    def test_once_per_turn(self):
        g, p, o = battle_ready()
        g.create_card(R_DECKHAND, p, "character")
        g.refresh_options()
        leader = g.players[p].leader
        g.step(opt(g, "activate", card=leader))
        g.step(opt(g, "select"))
        self.assertFalse(has_opt(g, "activate", card=leader))


class Battles(unittest.TestCase):
    def test_tie_goes_to_attacker(self):
        g, p, o = battle_ready()
        g.step(opt(g, "attack", card=g.players[p].leader, target=g.players[o].leader))
        self.assertEqual((len(g.players[o].life), len(g.players[o].hand)), (4, 1))

    def test_only_rested_characters_can_be_attacked(self):
        g, p, o = battle_ready()
        active = g.create_card(R_DECKHAND, o, "character")
        rested = g.create_card(R_DECKHAND, o, "character")
        rested.rested = True
        g.refresh_options()
        self.assertFalse(has_opt(g, "attack", target=active))
        self.assertTrue(has_opt(g, "attack", target=rested))

    def test_blocker_takes_the_hit(self):
        g, p, o = battle_ready()
        lookout = g.create_card(R_LOOKOUT, o, "character")
        g.refresh_options()
        g.step(opt(g, "attack", card=g.players[p].leader, target=g.players[o].leader))
        g.step(opt(g, "block", card=lookout))
        self.assertIn(lookout, g.players[o].trash)
        self.assertEqual(len(g.players[o].life), 5)

    def test_counter_wins_battle_and_expires(self):
        g, p, o = battle_ready()
        counter = g.create_card(R_DECKHAND, o, "hand")
        oleader = g.players[o].leader
        g.step(opt(g, "attack", card=g.players[p].leader, target=oleader))
        g.step(opt(g, "counter", card=counter))
        self.assertEqual(len(g.players[o].life), 5)
        self.assertIn(counter, g.players[o].trash)
        self.assertEqual(g.power(oleader), 5000)  # +1000 lasted only for that battle

    def test_double_attack(self):
        g, p, o = battle_ready()
        ff = g.create_card(R_FIRE_FIST, p, "character")
        ff.played_turn = 1
        g.refresh_options()
        g.step(opt(g, "attack", card=ff, target=g.players[o].leader))
        self.assertEqual(len(g.players[o].life), 3)

    def test_banish_trashes_life(self):
        g, p, o = battle_ready()
        tk = g.create_card(G_THORN, p, "character")
        tk.played_turn = 1
        g.refresh_options()
        g.step(opt(g, "attack", card=tk, target=g.players[o].leader))
        self.assertEqual((len(g.players[o].life), len(g.players[o].hand)), (4, 0))

    def test_trigger_plays_card(self):
        g, p, o = battle_ready()
        rookie = g.create_card(R_ROOKIE, o, "life")
        g.step(opt(g, "attack", card=g.players[p].leader, target=g.players[o].leader))
        self.assertEqual(g.pending.kind, "trigger")
        g.step(opt(g, "yes"))
        self.assertIn(rookie, g.players[o].characters)

    def test_damage_at_zero_life_loses(self):
        g, p, o = battle_ready()
        for c in list(g.players[o].life):
            g.move(c, "trash")
        g.step(opt(g, "attack", card=g.players[p].leader, target=g.players[o].leader))
        self.assertTrue(g.over)
        self.assertEqual(g.winner, p)

    def test_rush(self):
        g, p, o = battle_ready()
        brawler = g.create_card(R_BRAWLER, p, "hand")
        bruiser = g.create_card(R_BRUISER, p, "hand")
        g.players[p].don_active = 10
        g.refresh_options()
        g.step(opt(g, "play", card=brawler))
        g.step(opt(g, "play", card=bruiser))
        self.assertTrue(has_opt(g, "attack", card=brawler))
        self.assertFalse(has_opt(g, "attack", card=bruiser))

    def test_on_ko_draws(self):
        g, p, o = battle_ready()
        treant = g.create_card(G_TREANT, o, "character")
        treant.rested = True
        leader = g.players[p].leader
        g.players[p].don_active = 4  # a spare DON!! keeps the turn from auto-ending
        g.refresh_options()
        for _ in range(3):
            g.step(opt(g, "give_don", target=leader))
        g.step(opt(g, "attack", card=leader, target=treant))
        self.assertIn(treant, g.players[o].trash)
        self.assertEqual(len(g.players[o].hand), 2)


class RulesAndDecks(unittest.TestCase):
    def test_deck_out_loses(self):
        g, p, o = battle_ready()
        for c in list(g.players[o].deck):
            g.move(c, "trash")
        g.step(opt(g, "end"))
        self.assertEqual(g.winner, p)

    def test_static_power(self):
        banner = CardDef("T-1", "Banner", Category.STAGE, ("red",), cost=0,
                         static_power=lambda g, src, c: 1000 if c.owner == src.owner and c.category == Category.CHARACTER else 0)
        g, p, o = battle_ready()
        d = g.create_card(R_DECKHAND, p, "character")
        g.create_card(banner, p, "stage")
        self.assertEqual(g.power(d), 3000)
        self.assertEqual(g.power(g.players[p].leader), 5000)

    def test_deck_validation(self):
        with self.assertRaises(DeckError):
            Deck(RED_LEADER, RED_DECK.cards[:-1] + [G_TREANT]).validate()  # wrong color
        with self.assertRaises(DeckError):
            Deck(GREEN_LEADER, [G_TREANT] * 50).validate()  # too many copies
        RED_DECK.validate()
        GREEN_DECK.validate()

    def test_illegal_action_rejected(self):
        g = new_game()
        with self.assertRaises(ValueError):
            g.step(Action("attack", card=0, target=1))

    def test_many_bot_games_finish(self):
        for seed in range(150):
            agents = (RandomAgent(seed), GreedyAgent()) if seed % 2 else (GreedyAgent(), RandomAgent(seed))
            g = play_game((RED_DECK, GREEN_DECK), agents, seed=seed)
            self.assertTrue(g.over)
            for ps in g.players:
                self.assertLessEqual(len(ps.characters), 5)
                self.assertEqual(ps.don_on_field + ps.don_deck, 10)  # DON!! never created or lost


if __name__ == "__main__":
    unittest.main()
