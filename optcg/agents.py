"""Bots. An agent answers a Decision with one of its options."""
from __future__ import annotations

import random
from typing import Optional

from .actions import Action, Decision
from .cards import Category
from .engine import Game


class Agent:
    def act(self, game: Game, decision: Decision) -> Action:
        raise NotImplementedError


class RandomAgent(Agent):
    def __init__(self, seed: Optional[int] = None):
        self.rng = random.Random(seed)

    def act(self, game, decision):
        return self.rng.choice(decision.options)


class GreedyAgent(Agent):
    """Simple rule-of-thumb play. A baseline to beat, not a strong player.

    It reads only the board and its own hand, never hidden information.
    """

    def act(self, game, d):
        handler = getattr(self, f"_{d.kind}", None)
        choice = handler(game, d) if handler else None
        return choice if choice is not None else self._default(game, d)

    # -- setup / yes-no
    def _go_first(self, g, d):
        return _find(d, "first")

    def _mulligan(self, g, d):
        return _find(d, "keep")

    def _trigger(self, g, d):
        return _find(d, "yes")

    def _confirm(self, g, d):
        return _find(d, "yes")

    def _default(self, g, d):
        """Effect choices: harm the opponent's strongest card, help our own strongest card."""
        cards = [o for o in d.options if o.card is not None]
        if not cards:
            return d.options[0]
        theirs = [o for o in cards if g.cards[o.card].owner != d.player]
        pool = theirs or cards
        return max(pool, key=lambda o: _strength(g, g.cards[o.card]))

    def _select(self, g, d):
        if "Trash" in d.prompt:  # discard the cheapest card
            return min((o for o in d.options if o.card is not None),
                       key=lambda o: g.cards[o.card].d.cost)
        return None

    # -- main phase
    def _main(self, g, d):
        me = d.player
        ps = g.players[me]
        opp = g.players[1 - me]
        by_kind: dict[str, list[Action]] = {}
        for o in d.options:
            by_kind.setdefault(o.kind, []).append(o)

        plays = by_kind.get("play", [])
        chars = [o for o in plays if g.cards[o.card].category == Category.CHARACTER]
        if chars and len(ps.characters) < 5:
            return max(chars, key=lambda o: g.cost(g.cards[o.card]))
        events = [o for o in plays if g.cards[o.card].category == Category.EVENT]
        if events and opp.characters:
            return events[0]
        stages = [o for o in plays if g.cards[o.card].category == Category.STAGE]
        if stages and ps.stage is None:
            return stages[0]
        if by_kind.get("activate"):
            return by_kind["activate"][0]

        # Spend leftover DON!! on the leader so it can swing harder.
        gives = by_kind.get("give_don", [])
        if gives and not ps.leader.rested and g.turn_number > 2:
            return next(o for o in gives if o.target == ps.leader.uid)

        attacks = by_kind.get("attack", [])
        good = [o for o in attacks if g.power(g.cards[o.card]) >= g.power(g.cards[o.target])]
        on_leader = [o for o in good if o.target == opp.leader.uid]
        if on_leader:
            return max(on_leader, key=lambda o: g.power(g.cards[o.card]))
        if good:
            return max(good, key=lambda o: g.cost(g.cards[o.target]))
        return _find(d, "end")

    # -- defence
    def _block(self, g, d):
        b = g.battle
        life = len(g.players[d.player].life)
        blocks = [o for o in d.options if o.kind == "block"]
        survivors = [o for o in blocks if g.power(g.cards[o.card]) > g.power(b.attacker)]
        if survivors:
            return survivors[0]
        if b.target.category == Category.LEADER and life <= 2 and blocks:
            return min(blocks, key=lambda o: g.cost(g.cards[o.card]))
        return _find(d, "pass")

    def _counter(self, g, d):
        b = g.battle
        need = g.power(b.attacker) - g.power(b.target) + 1
        if need <= 0:
            return _find(d, "pass")
        life = len(g.players[d.player].life)
        worth_it = b.target.category == Category.CHARACTER and g.cost(b.target) >= 4 or \
            b.target.category == Category.LEADER and life <= 3
        counters = [o for o in d.options if o.kind == "counter" and o.target == b.target.uid]
        total = sum(g.counter_value(g.cards[o.card]) for o in counters)
        if not worth_it or total < need:
            return _find(d, "pass")
        enough = [o for o in counters if g.counter_value(g.cards[o.card]) >= need]
        if enough:
            return min(enough, key=lambda o: g.counter_value(g.cards[o.card]))
        return max(counters, key=lambda o: g.counter_value(g.cards[o.card]))


def _find(d: Decision, kind: str) -> Optional[Action]:
    return next((o for o in d.options if o.kind == kind), None)


def _strength(g: Game, card) -> int:
    return g.power(card) if card.category in (Category.LEADER, Category.CHARACTER) else card.d.cost * 1000


AGENTS = {"random": RandomAgent, "greedy": GreedyAgent}
