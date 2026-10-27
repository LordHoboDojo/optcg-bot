"""ONE PIECE CARD GAME rules engine (Comprehensive Rules v1.2.1).

The game runs as a generator. Whenever a player has to decide something the
engine stops on a Decision (`game.pending`); the caller answers with
`game.step(action)`, where action is one of `game.pending.options`. Card
effects are generators too, so they can ask questions halfway through.

Card-writing helpers (draw, ko, add_power, choose_cards, ...) are public
methods on Game; everything starting with an underscore is engine internals.
"""
from __future__ import annotations

import inspect
import random
from dataclasses import dataclass
from typing import Iterable, Optional

from .actions import Action, Decision
from .cards import (BANISH, BLOCKER, CANNOT_ATTACK, DOUBLE_ATTACK, NO_KO_BY_EFFECTS, NO_KO_BY_OPP_EFFECTS,
                    NO_KO_IN_BATTLE, RUSH, RUSH_CHARACTER, UNBLOCKABLE,
                    Card, CardDef, Category, Mod, Timing)
from .deck import Deck

FIELD_ZONES = ("leader", "character", "stage")
MAX_CHARACTERS = 5
DON_DECK_SIZE = 10
OPENING_HAND = 5


class GameOver(Exception):
    def __init__(self, winner: Optional[int]):
        super().__init__(winner)
        self.winner = winner


class IllegalAction(ValueError):
    pass


@dataclass
class _Pending:
    """An auto effect waiting to activate."""
    src: Card
    idx: int
    inc: int             # the source's incarnation when it triggered
    batch: int
    activated: bool = False  # already activated and paid for ([On K.O.])


@dataclass
class Battle:
    attacker: Card
    target: Card
    attacker_inc: int
    target_inc: int


class PlayerState:
    def __init__(self, idx: int):
        self.idx = idx
        self.leader: Card = None  # type: ignore[assignment]
        # For deck, life and trash the top card is the LAST element.
        self.deck: list[Card] = []
        self.hand: list[Card] = []
        self.trash: list[Card] = []
        self.life: list[Card] = []
        self.characters: list[Card] = []
        self.stage: Optional[Card] = None
        # DON!! cards are interchangeable, so they are tracked as counts.
        self.don_deck = DON_DECK_SIZE
        self.don_active = 0
        self.don_rested = 0
        self.no_character_play_turn = -1  # "you cannot play Character cards during this turn"

    @property
    def field(self) -> list[Card]:
        """Leader, Characters, then Stage."""
        return [self.leader, *self.characters] + ([self.stage] if self.stage else [])

    @property
    def don_on_field(self) -> int:
        return self.don_active + self.don_rested + sum(c.don for c in self.field)


class Game:
    def __init__(self, decks: tuple[Deck, Deck] | list[Deck], seed: Optional[int] = None):
        self.rng = random.Random(seed)
        self.players = [PlayerState(0), PlayerState(1)]
        self.cards: dict[int, Card] = {}
        self.turn_number = 0          # 1 = first player's first turn, 2 = second player's first turn
        self.turn_player = 0
        self.first_player = 0
        self.phase = "setup"
        self.battle: Optional[Battle] = None
        self.winner: Optional[int] = None
        self.over = False
        self.log: list[str] = []
        self.history: list[tuple[int, Action]] = []
        self.pending: Optional[Decision] = None
        self._in_static_cost = False
        self._queue: list[_Pending] = []
        self._resolving_batch: Optional[int] = None

        for i, deck in enumerate(decks):
            deck.validate()
            self.create_card(deck.leader, i, "leader")
            for d in deck.cards:
                self.create_card(d, i, "deck")
            self.rng.shuffle(self.players[i].deck)

        self._gen = self._run()
        self._advance(None)

    # ------------------------------------------------------------------ driving

    def legal_actions(self) -> tuple[Action, ...]:
        return self.pending.options if self.pending else ()

    def step(self, action: Action) -> None:
        if self.over or self.pending is None:
            raise IllegalAction("the game is over")
        if action not in self.pending.options:
            raise IllegalAction(f"{action!r} is not a legal option for {self.pending.kind}")
        self.history.append((self.pending.player, action))
        self._advance(action)

    def refresh_options(self) -> None:
        """Recompute the main-phase options after the state was edited by hand (tests, puzzles)."""
        if self.pending and self.pending.kind == "main":
            p = self.pending.player
            self.pending = Decision(p, "main", tuple(self._main_options(p)), self.pending.prompt)

    def _advance(self, value) -> None:
        try:
            self.pending = self._gen.send(value)
        except GameOver as e:
            self.winner = e.winner
            self.over = True
            self.pending = None
            self._log("Game over: " + ("draw" if e.winner is None else f"player {e.winner} wins"))

    def _log(self, msg: str) -> None:
        self.log.append(f"[T{self.turn_number}] {msg}")

    # ------------------------------------------------------------ choices

    def choose(self, player: int, kind: str, options: list[Action], prompt: str = ""):
        """Ask `player` to pick one option. A single option is picked automatically."""
        self._check_rules()
        if len(options) == 1:
            return options[0]
        return (yield Decision(player, kind, tuple(options), prompt))

    def confirm(self, player: int, prompt: str):
        a = yield from self.choose(player, "confirm", [Action("yes"), Action("no")], prompt)
        return a.kind == "yes"

    def choose_cards(self, player: int, candidates: Iterable[Card], up_to: int = 1,
                     prompt: str = "Choose a card", exactly: bool = False):
        """Pick up to `up_to` cards one at a time (with a Done option), or exactly that many
        when `exactly` is set. Returns the list of chosen cards."""
        cands = list(candidates)
        chosen: list[Card] = []
        while len(chosen) < up_to and cands:
            opts = [] if exactly else [Action("pass", label="Done")]
            opts += [Action("select", card=c.uid, label=self.describe(c)) for c in cands]
            a = yield from self.choose(player, "select", opts, prompt)
            if a.kind == "pass":
                break
            c = self.cards[a.card]
            chosen.append(c)
            cands.remove(c)
        return chosen

    # ------------------------------------------------------------ queries

    def opponent(self, player: int) -> int:
        return 1 - player

    def characters(self, player: int) -> list[Card]:
        return list(self.players[player].characters)

    def leader_and_characters(self, player: int) -> list[Card]:
        ps = self.players[player]
        return [ps.leader, *ps.characters]

    def negated(self, card: Card) -> bool:
        return any(m.kind == "negated" for m in card.mods)

    def negate(self, card: Card, until: str = "turn", by: Optional[int] = None) -> None:
        """The card's own effects (keywords included) stop working."""
        card.mods.append(Mod("negated", True, until, self.turn_player if by is None else by))

    def _statics(self, attr: str):
        """(source, fn) for every permanent effect of this kind on the field."""
        for ps in self.players:
            for src in ps.field:
                fn = getattr(src.d, attr)
                if fn and not self.negated(src):
                    yield src, fn

    def has_keyword(self, card: Card, keyword: str) -> bool:
        if keyword in card.d.keywords and not self.negated(card):
            return True
        if any(m.kind == "keyword" and m.value == keyword for m in card.mods):
            return True
        return any(keyword in fn(self, src, card) for src, fn in self._statics("static_keywords"))

    def base_power(self, card: Card) -> int:
        bases = [m.value for m in card.mods if m.kind == "base_power"]
        bases += [v for src, fn in self._statics("static_base_power") if (v := fn(self, src, card)) is not None]
        return max(bases) if bases else card.d.power  # 4-9-2-1: highest value wins

    def power(self, card: Card) -> int:
        p = self.base_power(card) + sum(m.value for m in card.mods if m.kind == "power")
        if card.don and self.turn_player == card.owner:  # 6-5-5-2
            p += 1000 * card.don
        p += sum(fn(self, src, card) for src, fn in self._statics("static_power"))
        return p  # may be negative (1-3-6-1)

    def cost(self, card: Card) -> int:
        c = card.d.cost + sum(m.value for m in card.mods if m.kind == "cost")
        if not self._in_static_cost:  # a cost effect whose condition reads costs sees printed ones
            self._in_static_cost = True
            try:
                c += sum(fn(self, src, card) for src, fn in self._statics("static_cost"))
                if card.zone == "hand" and card.d.hand_cost:
                    c += card.d.hand_cost(self, card)
            finally:
                self._in_static_cost = False
        return max(0, c)

    def counter_value(self, card: Card) -> int:
        """2-10-4: with several Counters, the highest applies."""
        values = [card.d.counter]
        if card.zone == "hand" and card.d.hand_counter:
            values.append(card.d.hand_counter(self, card) or 0)
        values += [fn(self, src, card) or 0 for src, fn in self._statics("static_counter")]
        return max(values)

    def describe(self, card: Card) -> str:
        d = card.d
        bits = [d.name]
        if d.category in (Category.CHARACTER, Category.EVENT, Category.STAGE):
            bits.append(f"cost {self.cost(card)}")
        if d.category in (Category.LEADER, Category.CHARACTER):
            bits.append(f"{self.power(card)} power")
        if card.rested:
            bits.append("rested")
        return f"{bits[0]} ({', '.join(bits[1:])})" if len(bits) > 1 else bits[0]

    def observe(self, player: int) -> dict:
        """What `player` is allowed to see (3-1-4, 3-1-5). Bots should decide from this."""
        def field_card(c: Card) -> dict:
            return {"uid": c.uid, "id": c.d.id, "name": c.name, "power": self.power(c),
                    "cost": self.cost(c), "rested": c.rested, "don": c.don,
                    "played_turn": c.played_turn}

        def side(p: int) -> dict:
            ps = self.players[p]
            out = {
                "leader": field_card(ps.leader),
                "characters": [field_card(c) for c in ps.characters],
                "stage": field_card(ps.stage) if ps.stage else None,
                "hand_size": len(ps.hand), "deck_size": len(ps.deck), "life": len(ps.life),
                # face-up Life cards are open information; index 0 is the bottom
                "life_face_up": {i: c.d.id for i, c in enumerate(ps.life) if c.face_up},
                "trash": [c.d.id for c in ps.trash],
                "don_deck": ps.don_deck, "don_active": ps.don_active, "don_rested": ps.don_rested,
            }
            if p == player:
                out["hand"] = [{"uid": c.uid, "id": c.d.id, "name": c.name} for c in ps.hand]
            return out

        return {"turn": self.turn_number, "turn_player": self.turn_player, "phase": self.phase,
                "me": side(player), "opponent": side(1 - player)}

    # ------------------------------------------------------------ card movement

    def create_card(self, definition: CardDef, owner: int, zone: Optional[str]) -> Card:
        card = Card(len(self.cards), definition, owner)
        self.cards[card.uid] = card
        if zone == "leader":
            self.players[owner].leader = card
            card.zone = "leader"
        elif zone is not None:
            self.move(card, zone)
        return card

    def _zone_list(self, card: Card, zone: Optional[str]) -> Optional[list[Card]]:
        ps = self.players[card.owner]
        return {"deck": ps.deck, "hand": ps.hand, "trash": ps.trash, "life": ps.life,
                "character": ps.characters}.get(zone)  # type: ignore[arg-type]

    def move(self, card: Card, zone: Optional[str], bottom: bool = False, face_up: bool = False) -> None:
        """Move a card to its owner's zone. zone=None puts it in limbo (a [Trigger] resolving)."""
        if card.zone == "leader":
            return  # Leaders never leave the Leader area (3-6-3)
        ps = self.players[card.owner]
        old = card.zone
        lst = self._zone_list(card, old)
        if lst is not None:
            lst.remove(card)
        elif old == "stage":
            ps.stage = None
        if card.don:  # 6-5-5-4: given DON!! go back to the cost area rested
            ps.don_rested += card.don
            card.don = 0
        card.reset()
        card.zone = zone
        card.face_up = face_up and zone == "life"
        if zone == "stage":
            ps.stage = card
        elif zone is not None:
            lst = self._zone_list(card, zone)
            if lst is None:
                raise ValueError(f"unknown zone {zone}")
            if bottom:
                lst.insert(0, card)
            else:
                lst.append(card)

    # ------------------------------------------------------------ effect helpers

    def draw(self, player: int, n: int = 1) -> None:
        ps = self.players[player]
        for _ in range(n):
            if not ps.deck:
                return
            self.move(ps.deck[-1], "hand")

    def add_power(self, card: Card, amount: int, until: str = "turn", by: Optional[int] = None) -> None:
        card.mods.append(Mod("power", amount, until, self.turn_player if by is None else by))

    def add_cost(self, card: Card, amount: int, until: str = "turn", by: Optional[int] = None) -> None:
        card.mods.append(Mod("cost", amount, until, self.turn_player if by is None else by))

    def grant_keyword(self, card: Card, keyword: str, until: str = "turn", by: Optional[int] = None) -> None:
        card.mods.append(Mod("keyword", keyword, until, self.turn_player if by is None else by))

    def set_base_power(self, card: Card, value: int, until: str = "next_end", by: Optional[int] = None) -> None:
        card.mods.append(Mod("base_power", value, until, self.turn_player if by is None else by))

    def skip_next_refresh(self, card: Card, by: int) -> None:
        """The card won't become active in its owner's next Refresh Phase."""
        card.mods.append(Mod("no_refresh", True, "permanent", by))

    def rest(self, card: Card) -> None:
        card.rested = True

    def set_active(self, card: Card) -> None:
        card.rested = False

    def return_to_hand(self, card: Card) -> None:
        self.move(card, "hand")

    def trash_from_hand(self, player: int, n: int = 1):
        cards = yield from self.choose_cards(player, self.players[player].hand, n,
                                             f"Trash {n} card(s) from your hand", exactly=True)
        for c in cards:
            self.move(c, "trash")
        return cards

    def add_don(self, player: int, n: int = 1, rested: bool = False) -> int:
        """Move up to n DON!! from the DON!! deck to the cost area. Returns how many moved."""
        ps = self.players[player]
        n = min(n, ps.don_deck)
        ps.don_deck -= n
        if rested:
            ps.don_rested += n
        else:
            ps.don_active += n
        return n

    def give_don(self, player: int, target: Card, n: int = 1, from_rested: bool = False) -> int:
        ps = self.players[player]
        if from_rested:
            n = min(n, ps.don_rested)
            ps.don_rested -= n
        else:
            n = min(n, ps.don_active)
            ps.don_active -= n
        target.don += n
        return n

    def _replaced(self, card: Card, by: Optional[int], ko: bool):
        """Offer the owner's replacement effects for `card` leaving the field. True if one was used."""
        for src in list(self.players[card.owner].field):
            if self.negated(src):
                continue
            for r in src.d.replacements:
                if not (r.applies(self, src, card, by, ko) and r.payable(self, src)):
                    continue
                if (yield from self.confirm(card.owner, f"{src.name}: {r.text} instead?")):
                    self._log(f"{src.name} replaces the removal of {card.name}")
                    yield from r.pay(self, src, card)
                    return True
        return False

    def remove_from_field(self, card: Card, zone: str, by: Optional[int], bottom: bool = False,
                          face_up: bool = False):
        """Move a Character or Stage off the field by an effect of player `by` (not a K.O.).
        Replacement effects may stop it. Returns True if it moved."""
        if card.zone not in ("character", "stage"):
            return False
        if (yield from self._replaced(card, by, ko=False)):
            return False
        self.move(card, zone, bottom=bottom, face_up=face_up)
        return True

    def place_in_order(self, player: int, cards: list[Card], zone: str, bottom: bool = False):
        """3-1-7: when several cards go to an area at once, their owner picks the order,
        from the top of the placed group down."""
        left, order = list(cards), []
        while len(left) > 1:
            opts = [Action("select", card=c.uid, label=c.name) for c in left]
            a = yield from self.choose(player, "order_cards", opts,
                                       f"Order the cards going to your {zone}: pick the next one from the top")
            order.append(self.cards[a.card])
            left.remove(order[-1])
        order += left
        if bottom:  # each later card goes under the earlier ones
            for c in order:
                self.move(c, zone, bottom=True)
        else:  # place bottom-most first so the first pick ends up on top
            for c in reversed(order):
                self.move(c, zone)

    def concede(self, player: int) -> None:
        """1-2-3: a player may concede at any time and loses immediately."""
        if self.over:
            raise IllegalAction("the game is over")
        self._gen.close()
        self.winner, self.over, self.pending = 1 - player, True, None
        self._log(f"Player {player} concedes. Game over: player {1 - player} wins")

    def rest_your_cards(self, player: int, n: int):
        """Cost "rest N of your cards": any active Leader, Character, Stage or DON!!."""
        ps = self.players[player]
        for _ in range(n):
            opts = [Action("select", card=c.uid, label=f"Rest {c.name}") for c in ps.field if not c.rested]
            if ps.don_active:
                opts.append(Action("select", index=0, label="Rest 1 active DON!!"))
            if not opts:
                return
            a = yield from self.choose(player, "select", opts, "Rest 1 of your cards")
            if a.card is None:
                ps.don_active -= 1
                ps.don_rested += 1
            else:
                self.cards[a.card].rested = True

    def active_card_count(self, player: int) -> int:
        ps = self.players[player]
        return sum(1 for c in ps.field if not c.rested) + ps.don_active

    def forbid_character_play(self, player: int) -> None:
        self.players[player].no_character_play_turn = self.turn_number

    def _ko_prevented(self, card: Card, by: Optional[int], in_battle: bool) -> bool:
        """10-2-1-3: "cannot be K.O.'d" applies only to K.O.s by battle or by effects."""
        if in_battle:
            return self.has_keyword(card, NO_KO_IN_BATTLE)
        if self.has_keyword(card, NO_KO_BY_EFFECTS):
            return True
        return by == 1 - card.owner and self.has_keyword(card, NO_KO_BY_OPP_EFFECTS)

    def ko(self, card: Card, by: Optional[int] = None, replaceable: bool = True, in_battle: bool = False):
        """K.O. a Character (10-2-1), by battle or by an effect of player `by`.
        [On K.O.] effects activate on the field and resolve from the trash (10-2-17)."""
        if card.zone != "character":
            return
        if self._ko_prevented(card, by, in_battle):
            self._log(f"{card.name} cannot be K.O.'d")
            return
        if replaceable and (yield from self._replaced(card, by, ko=True)):
            return
        ready = [i for i in card.d.effect_indexes(Timing.ON_KO) if self.can_activate(card, i)]
        self._log(f"{card.name} is K.O.'d")
        paid = []
        for i in ready:  # activate while the card is still on the field
            eff = card.d.effects[i]
            if eff.optional and not (yield from self.confirm(card.owner, f"Activate {card.name}: {eff.text}?")):
                continue
            if (yield from self._pay_costs(card, i)):
                paid.append(i)
        self.move(card, "trash")
        for i in paid:  # resolved from the trash, in turn order with other triggered effects
            self._queue.append(_Pending(card, i, card.incarnation, self._next_batch(), activated=True))

    def play_card(self, card: Card, rested: bool = False):
        """Play a Character or Stage without paying its cost (e.g. from an effect)."""
        if card.category == Category.CHARACTER:
            yield from self._play_character(card, rested)
        elif card.category == Category.STAGE:
            yield from self._play_stage(card)
        else:
            raise ValueError(f"cannot play {card.category}")

    def deal_damage(self, player: int, amount: int = 1, banish: bool = False):
        """Damage processing (4-6, 7-1-4-1-1)."""
        ps = self.players[player]
        for _ in range(amount):
            if not ps.life:
                self._log(f"Player {player} takes damage with 0 Life")
                raise GameOver(1 - player)
            card = ps.life[-1]
            if banish:
                self.move(card, "trash")
                self._log(f"Player {player}'s Life card is trashed (Banish)")
                continue
            trig = [i for i in card.d.effect_indexes(Timing.TRIGGER) if self.can_activate(card, i)]
            if trig and (yield from self.choose(player, "trigger",
                                                [Action("yes", card=card.uid, label=f"Activate [Trigger] of {card.name}"),
                                                 Action("no", label="Add to hand")],
                                                f"{card.name} has [Trigger]")).kind == "yes":
                self.move(card, None)  # belongs to no area while its [Trigger] resolves (10-1-5-3)
                self._log(f"Player {player} activates [Trigger] of {card.name}")
                yield from self._resolve(card, trig[0])
                if card.zone is None:
                    self.move(card, "trash")
            else:
                self.move(card, "hand")
                self._log(f"Player {player} adds a Life card to hand ({len(ps.life)} left)")

    # ------------------------------------------------------------ effects

    def can_activate(self, src: Card, idx: int) -> bool:
        eff = src.d.effects[idx]
        p = src.owner
        ps = self.players[p]
        if self.negated(src):
            return False
        if eff.your_turn and self.turn_player != p:
            return False
        if eff.opp_turn and self.turn_player == p:
            return False
        if eff.don_req and src.don < eff.don_req:
            return False
        if eff.once_per_turn and src.opt_used.get(eff.opt_group or idx) == self.turn_number:
            return False
        if eff.rest_self and src.rested:
            return False
        if eff.rest_don > ps.don_active:
            return False
        if eff.don_minus > ps.don_on_field:
            return False
        if eff.condition and not eff.condition(self, src):
            return False
        return True

    def _pay_costs(self, src: Card, idx: int):
        """Pay an effect's activation cost (8-3). Returns True if paid."""
        eff = src.d.effects[idx]
        ps = self.players[src.owner]
        if not self.can_activate(src, idx):
            return False
        if eff.once_per_turn:
            src.opt_used[eff.opt_group or idx] = self.turn_number
        if eff.rest_self:
            src.rested = True
        if eff.rest_don:
            ps.don_active -= eff.rest_don
            ps.don_rested += eff.rest_don
        if eff.don_minus:
            yield from self.return_don(src.owner, eff.don_minus)
        return True

    def return_don(self, player: int, n: int):
        """DON!! -X (8-3-1-6, 10-2-10): the player picks X DON!! from their cost area, Leader and
        Characters and returns them to the DON!! deck."""
        ps = self.players[player]
        for _ in range(n):
            opts = []
            if ps.don_rested:
                opts.append(Action("select", index=0, label="Rested DON!! in your cost area"))
            if ps.don_active:
                opts.append(Action("select", index=1, label="Active DON!! in your cost area"))
            opts += [Action("select", card=c.uid, label=f"DON!! given to {c.name}") for c in ps.field if c.don]
            if not opts:
                break
            a = yield from self.choose(player, "return_don", opts, "Return 1 DON!! to your DON!! deck")
            if a.card is not None:
                self.cards[a.card].don -= 1
            elif a.index == 0:
                ps.don_rested -= 1
            else:
                ps.don_active -= 1
            ps.don_deck += 1
        self.fire(Timing.DON_RETURNED, ps.field)

    def _resolve(self, src: Card, idx: int):
        eff = src.d.effects[idx]
        text = eff.text if eff.text.startswith("[") else f"[{eff.timing.value}] {eff.text}"
        self._log(f"{src.name}: {text}".rstrip())
        result = eff.resolve(self, src)
        if inspect.isgenerator(result):
            yield from result

    def activate(self, src: Card, idx: int):
        if (yield from self._pay_costs(src, idx)):
            yield from self._resolve(src, idx)

    # Triggered (auto) effects don't resolve on the spot. fire() queues them and the game
    # resolves the queue at the next safe point (8-6):
    #   - effects triggered together form a batch; the turn player resolves theirs first, choosing
    #     the order, then the non-turn player (8-6-1);
    #   - effects triggered while a batch resolves wait for the next batch, so they go after the
    #     rest of the current batch (8-6-1-1) and after the effect that caused them (8-6-3);
    #   - effects triggered during damage processing wait until it is over (8-6-2).

    def _next_batch(self) -> int:
        return 0 if self._resolving_batch is None else self._resolving_batch + 1

    def fire(self, timing: Timing, sources: Iterable[Card]) -> None:
        """Queue every auto effect with this timing on these cards whose conditions are met now."""
        for src in sources:
            for i in src.d.effect_indexes(timing):
                if self.can_activate(src, i):
                    self._queue.append(_Pending(src, i, src.incarnation, self._next_batch()))

    def resolve_triggers(self):
        """Resolve queued auto effects until none are left."""
        while self._queue:
            batch = min(q.batch for q in self._queue)
            tp = self.turn_player
            mine = [q for q in self._queue if q.batch == batch and q.src.owner == tp] or \
                [q for q in self._queue if q.batch == batch]
            q = mine[0]
            if len(mine) > 1:
                opts = [Action("select", card=x.src.uid, index=x.idx,
                               label=f"{x.src.name}: {x.src.d.effects[x.idx].text}") for x in mine]
                a = yield from self.choose(q.src.owner, "order", opts, "Choose which effect resolves next")
                q = next(x for x in mine if x.src.uid == a.card and x.idx == a.index)
            self._queue.remove(q)
            outer, self._resolving_batch = self._resolving_batch, batch
            try:
                yield from self._activate_pending(q)
            finally:
                self._resolving_batch = outer

    def _activate_pending(self, q: "_Pending"):
        src, eff = q.src, q.src.d.effects[q.idx]
        if q.activated:  # [On K.O.]: activated on the field, resolves from the trash (10-2-17)
            yield from self._resolve(src, q.idx)
            return
        if src.incarnation != q.inc:  # the card moved before its effect activated (8-1-3-1-3)
            return
        if not self.can_activate(src, q.idx):
            return
        if eff.optional and not (yield from self.confirm(src.owner, f"Activate {src.name}: {eff.text}?")):
            return
        yield from self.activate(src, q.idx)

    def _ordered_field(self) -> list[Card]:
        tp = self.turn_player
        return self.players[tp].field + self.players[1 - tp].field

    def _expire(self, until: str, player: Optional[int] = None) -> None:
        for c in self.cards.values():
            if c.mods:
                c.mods = [m for m in c.mods if not (m.until == until and (player is None or m.player == player))]

    # ------------------------------------------------------------ rule processing

    def _check_rules(self) -> None:
        """Defeat judgment (9-2): a player with 0 cards in deck loses."""
        losers = [p for p in (0, 1) if not self.players[p].deck]
        if len(losers) == 2:
            raise GameOver(None)
        if losers:
            self._log(f"Player {losers[0]} has no cards in deck")
            raise GameOver(1 - losers[0])

    # ------------------------------------------------------------ game flow

    def _run(self):
        yield from self._setup()
        while True:
            yield from self._turn()
            self.turn_player = 1 - self.turn_player

    def _setup(self):
        chooser = self.rng.randrange(2)
        a = yield from self.choose(chooser, "go_first", [Action("first"), Action("second")],
                                   "Go first or second?")
        self.first_player = chooser if a.kind == "first" else 1 - chooser
        self.turn_player = self.first_player
        order = [self.first_player, 1 - self.first_player]
        self._log(f"Player {self.first_player} goes first")

        # 5-2-1-5-1: the player who chose resolves "at the start of the game" first.
        for p in (chooser, 1 - chooser):
            self.fire(Timing.START_OF_GAME, [self.players[p].leader])
            yield from self.resolve_triggers()
            self.rng.shuffle(self.players[p].deck)

        for p in order:
            self.draw(p, OPENING_HAND)
        for p in order:
            a = yield from self.choose(p, "mulligan", [Action("keep"), Action("mulligan")],
                                       "Keep this hand or redraw once?")
            if a.kind == "mulligan":
                ps = self.players[p]
                for c in list(ps.hand):
                    self.move(c, "deck")
                self.rng.shuffle(ps.deck)
                self.draw(p, OPENING_HAND)
                self._log(f"Player {p} redraws")
        for p in order:  # 5-2-1-7: top card of the deck ends up at the bottom of Life
            ps = self.players[p]
            for _ in range(ps.leader.d.life):
                self.move(ps.deck[-1], "life")

    def _turn(self):
        p = self.turn_player
        ps = self.players[p]
        self.turn_number += 1
        self._log(f"--- Player {p}'s turn ---")

        # Refresh Phase (6-2)
        self.phase = "refresh"
        self._expire("next_turn", p)
        self.fire(Timing.START_OF_TURN, self._ordered_field())  # 6-2-2
        yield from self.resolve_triggers()
        for c in ps.field:
            ps.don_rested += c.don
            c.don = 0
        for c in ps.field:
            if any(m.kind == "no_refresh" for m in c.mods):
                c.mods = [m for m in c.mods if m.kind != "no_refresh"]
            else:
                c.rested = False
        ps.don_active += ps.don_rested
        ps.don_rested = 0

        # Draw Phase (6-3)
        self.phase = "draw"
        if self.turn_number != 1:
            self.draw(p, 1)

        # DON!! Phase (6-4)
        self.phase = "don"
        self.add_don(p, 1 if self.turn_number == 1 else 2)

        # Main Phase (6-5)
        self.phase = "main"
        self.fire(Timing.START_OF_MAIN, self._ordered_field())  # 6-5-1
        yield from self.resolve_triggers()
        while True:
            a = yield from self.choose(p, "main", self._main_options(p), "Main Phase")
            if a.kind == "end":
                break
            yield from self._do_main_action(p, a)
            yield from self.resolve_triggers()
            self._check_rules()

        # End Phase (6-6)
        self.phase = "end"
        self.fire(Timing.END_OF_YOUR_TURN, ps.field)  # 6-6-1-1-2: all of these first
        yield from self.resolve_triggers()
        self.fire(Timing.END_OF_OPP_TURN, self.players[1 - p].field)
        yield from self.resolve_triggers()
        self._expire("turn")
        self._expire("next_end", 1 - p)  # "until the end of your opponent's next End Phase"
        self._check_rules()

    def _main_options(self, p: int) -> list[Action]:
        ps = self.players[p]
        opts: list[Action] = []
        for c in ps.hand:
            cost = self.cost(c)
            if cost > ps.don_active:
                continue
            if c.category == Category.CHARACTER and ps.no_character_play_turn == self.turn_number:
                continue
            if c.category in (Category.CHARACTER, Category.STAGE):
                opts.append(Action("play", card=c.uid, label=f"Play {self.describe(c)}"))
            elif c.category == Category.EVENT:
                main = c.d.effect_indexes(Timing.MAIN)
                if main and self.can_activate(c, main[0]):
                    opts.append(Action("play", card=c.uid, label=f"Activate {self.describe(c)}"))
        for c in ps.field:
            for i in c.d.effect_indexes(Timing.ACTIVATE_MAIN):
                if self.can_activate(c, i):
                    opts.append(Action("activate", card=c.uid, index=i,
                                       label=f"Activate {c.name}: {c.d.effects[i].text}"))
        if ps.don_active:
            for c in self.leader_and_characters(p):
                opts.append(Action("give_don", target=c.uid, label=f"Give 1 DON!! to {c.name}"))
        if self.turn_number > 2:  # 6-5-6-1: nobody battles on their first turn
            for atk in self.leader_and_characters(p):
                if atk.rested or self.has_keyword(atk, CANNOT_ATTACK):
                    continue
                for tgt in self._attack_targets(atk):
                    opts.append(Action("attack", card=atk.uid, target=tgt.uid,
                                       label=f"{atk.name} ({self.power(atk)}) attacks {self.describe(tgt)}"))
        opts.append(Action("end", label="End turn"))
        return opts

    def _attack_targets(self, atk: Card) -> list[Card]:
        opp = self.players[1 - atk.owner]
        targets = [opp.leader] + [c for c in opp.characters if c.rested]
        locks = {src.name for src, fn in self._statics("attack_lock") if src.owner != atk.owner and fn(self, src)}
        if locks:  # "your opponent cannot attack any card other than the Character [X]"
            targets = [c for c in targets if c.category == Category.CHARACTER and c.name in locks]
        if atk.category == Category.CHARACTER and atk.played_turn == self.turn_number:
            if self.has_keyword(atk, RUSH):
                return targets
            if self.has_keyword(atk, RUSH_CHARACTER):
                return [c for c in targets if c.category == Category.CHARACTER]
            return []  # 3-7-4
        return targets

    def _do_main_action(self, p: int, a: Action):
        ps = self.players[p]
        if a.kind == "play":
            card = self.cards[a.card]
            self._pay_card_cost(card)
            if card.category == Category.EVENT:
                yield from self._activate_event(card, Timing.MAIN)
            else:
                yield from self.play_card(card)
        elif a.kind == "activate":
            yield from self.activate(self.cards[a.card], a.index)
        elif a.kind == "give_don":
            self.give_don(p, self.cards[a.target], 1)
        elif a.kind == "attack":
            yield from self._battle(self.cards[a.card], self.cards[a.target])

    def _pay_card_cost(self, card: Card) -> None:
        ps = self.players[card.owner]
        cost = self.cost(card)
        ps.don_active -= cost
        ps.don_rested += cost

    def _activate_event(self, card: Card, timing: Timing):
        """8-4-2: trash the Event, then carry out its effect."""
        idx = card.d.effect_indexes(timing)[0]
        self._log(f"Player {card.owner} activates {card.name}")
        self.move(card, "trash")
        yield from self.activate(card, idx)

    def _play_character(self, card: Card, rested: bool = False):
        ps = self.players[card.owner]
        if ps.no_character_play_turn == self.turn_number:
            return
        if len(ps.characters) >= MAX_CHARACTERS:  # 3-7-6-1: rule processing, not a K.O.
            victim, = yield from self.choose_cards(card.owner, ps.characters, 1,
                                                   "Character area is full: trash 1", exactly=True)
            self.move(victim, "trash")
            self._log(f"{victim.name} is trashed to make room")
        self.move(card, "character")
        card.rested = rested
        card.played_turn = self.turn_number
        self._log(f"Player {card.owner} plays {card.name}")
        self.fire(Timing.ON_PLAY, [card])

    def _play_stage(self, card: Card):
        ps = self.players[card.owner]
        if ps.stage:
            self.move(ps.stage, "trash")
        self.move(card, "stage")
        card.played_turn = self.turn_number
        self._log(f"Player {card.owner} plays {card.name}")
        self.fire(Timing.ON_PLAY, [card])
        yield from ()

    # ------------------------------------------------------------ battle (7)

    def _battle_interrupted(self, b: Battle) -> bool:
        return b.attacker.incarnation != b.attacker_inc or b.target.incarnation != b.target_inc

    def _battle(self, attacker: Card, target: Card):
        b = Battle(attacker, target, attacker.incarnation, target.incarnation)
        self.battle = b
        try:
            yield from self._battle_steps(b)
            # End of the Battle (7-1-5), also reached when a step was cut short
            self.phase = "end_of_battle"
            self.fire(Timing.END_OF_BATTLE, self._ordered_field())
            yield from self.resolve_triggers()
        finally:
            self._expire("battle")
            self.battle = None
            self.phase = "main"

    def _battle_steps(self, b: Battle):
        attacker = b.attacker
        o = 1 - attacker.owner
        opp = self.players[o]
        # Attack Step
        self.phase = "attack"
        attacker.rested = True
        self._log(f"{attacker.name} ({self.power(attacker)}) attacks {self.describe(b.target)}")
        self.fire(Timing.WHEN_ATTACKING, [attacker])
        self.fire(Timing.ON_OPP_ATTACK, opp.field)  # 10-2-16-1: after [When Attacking]
        self.fire(Timing.ATTACK_DECLARED, self._ordered_field())
        yield from self.resolve_triggers()
        if self._battle_interrupted(b):  # 7-1-1-4
            return

        # Block Step
        self.phase = "block"
        if not self.has_keyword(attacker, UNBLOCKABLE):
            blockers = [c for c in opp.characters
                        if c is not b.target and not c.rested and self.has_keyword(c, BLOCKER)]
            if blockers:
                opts = [Action("pass", label="Don't block")] + [
                    Action("block", card=c.uid, label=f"Block with {self.describe(c)}") for c in blockers]
                a = yield from self.choose(o, "block", opts,
                                           f"{attacker.name} ({self.power(attacker)}) attacks {self.describe(b.target)}")
                if a.kind == "block":
                    blocker = self.cards[a.card]
                    blocker.rested = True
                    b.target, b.target_inc = blocker, blocker.incarnation
                    self._log(f"{blocker.name} blocks")
                    self.fire(Timing.ON_BLOCK, [blocker])
                    yield from self.resolve_triggers()
        if self._battle_interrupted(b):  # 7-1-2-3
            return

        # Counter Step
        self.phase = "counter"
        while True:
            opts = [Action("pass", label="No more counters")]
            for c in opp.hand:
                if c.category == Category.CHARACTER and self.counter_value(c) > 0:
                    # 7-1-3-1-1: boost the Leader or any 1 Character, the battling card first
                    targets = [b.target] + [t for t in self.leader_and_characters(o) if t is not b.target]
                    opts += [Action("counter", card=c.uid, target=t.uid,
                                    label=f"Counter +{self.counter_value(c)} on {t.name} with {c.name}")
                             for t in targets]
                elif c.category == Category.EVENT and self.cost(c) <= opp.don_active:
                    idx = c.d.effect_indexes(Timing.COUNTER)
                    if idx and self.can_activate(c, idx[0]):
                        opts.append(Action("event", card=c.uid, label=f"Activate {self.describe(c)}"))
            a = yield from self.choose(o, "counter", opts,
                                       f"{attacker.name} ({self.power(attacker)}) vs {self.describe(b.target)}")
            if a.kind == "pass":
                break
            card = self.cards[a.card]
            if a.kind == "counter":
                value, boosted = self.counter_value(card), self.cards[a.target]
                self.move(card, "trash")
                self.add_power(boosted, value, "battle", by=o)
                self._log(f"Player {o} counters +{value} on {boosted.name} with {card.name}")
            else:
                self._pay_card_cost(card)
                yield from self._activate_event(card, Timing.COUNTER)
            yield from self.resolve_triggers()
            if self._battle_interrupted(b):  # 7-1-3-1-3
                return

        # Damage Step
        self.phase = "damage"
        atk_power, tgt_power = self.power(attacker), self.power(b.target)
        if atk_power >= tgt_power:
            if b.target.category == Category.LEADER:
                dmg = 2 if self.has_keyword(attacker, DOUBLE_ATTACK) else 1
                yield from self.deal_damage(o, dmg, banish=self.has_keyword(attacker, BANISH))
            else:
                yield from self.ko(b.target, by=None, in_battle=True)
        else:
            self._log(f"Attack fails ({atk_power} < {tgt_power})")
        yield from self.resolve_triggers()  # 8-6-2: after all damage processing
