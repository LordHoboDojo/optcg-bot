"""Play against a bot in the terminal.

    python3 -m optcg.play --deck0 "(Blue) Rocks.D.Xebec" --deck1 "(Yellow) Charlotte Linlin" --human 0

At each decision you see what happened since your last one, the board (only what you are
allowed to see), and numbered options. Type a number to choose, or:
    t N     show the text of the card in option N
    b       show the board again
    log     show the whole game log
    q       concede
"""
from __future__ import annotations

from typing import Callable

from .actions import Action, Decision
from .agents import Agent
from .cards import CANNOT_ATTACK, KEYWORDS, Card, Category
from .engine import Game

BOARD_KINDS = {"main", "block", "counter", "trigger", "mulligan"}
HELP = "Type an option number, 't N' for card text, 'b' for the board, 'log' for the full log, 'q' to concede."


class Concede(Exception):
    pass


def _flags(g: Game, c: Card) -> str:
    bits = []
    if c.rested:
        bits.append("rested")
    if c.don:
        bits.append(f"+{c.don} DON!!")
    bits += [k for k in sorted(KEYWORDS) if g.has_keyword(c, k)]
    if g.has_keyword(c, CANNOT_ATTACK):
        bits.append("can't attack")
    return f" [{', '.join(bits)}]" if bits else ""


def _field_card(g: Game, c: Card) -> str:
    if c.category == Category.STAGE:
        return f"{c.name} (cost {g.cost(c)}){_flags(g, c)}"
    cost = f"cost {g.cost(c)}, " if c.category == Category.CHARACTER else ""
    return f"{c.name} ({cost}{g.power(c)} power){_flags(g, c)}"


def _hand_card(g: Game, c: Card) -> str:
    d = c.d
    stats = [f"cost {g.cost(c)}"]
    if d.category == Category.CHARACTER:
        stats.append(f"{d.power} power")
        if g.counter_value(c):
            stats.append(f"counter +{g.counter_value(c)}")
    if d.trigger_text:
        stats.append("Trigger")
    return f"{c.name} [{d.category.value}] ({', '.join(stats)})"


def render_board(g: Game, me: int) -> str:
    out = []

    def side(p: int, title: str) -> None:
        ps = g.players[p]
        face_up = [c.name for c in ps.life if c.face_up]
        life = f"{len(ps.life)}" + (f" (face-up: {', '.join(face_up)})" if face_up else "")
        trash_top = f", top: {ps.trash[-1].name}" if ps.trash else ""
        out.append(f"== {title} ==  Life {life} | Hand {len(ps.hand)} | Deck {len(ps.deck)} | "
                   f"Trash {len(ps.trash)}{trash_top}")
        out.append(f"   DON!!: {ps.don_active} active, {ps.don_rested} rested, {ps.don_deck} in DON!! deck")
        out.append(f"   Leader: {_field_card(g, ps.leader)}")
        if ps.characters:
            out.append("   Characters:")
            out.extend(f"     - {_field_card(g, c)}" for c in ps.characters)
        else:
            out.append("   Characters: none")
        if ps.stage:
            out.append(f"   Stage: {_field_card(g, ps.stage)}")
        if p == me:
            out.append("   Your hand:")
            out.extend([f"     - {_hand_card(g, c)}" for c in ps.hand] or ["     (empty)"])

    turn = "your" if g.turn_player == me else "opponent's"
    out.append(f"\n------------------- Turn {g.turn_number} ({turn} turn) -------------------")
    side(1 - me, "OPPONENT")
    if g.battle:
        b = g.battle
        out.append(f"   >>> Battle: {b.attacker.name} ({g.power(b.attacker)}) attacking "
                   f"{b.target.name} ({g.power(b.target)})")
    side(me, "YOU")
    return "\n".join(out)


def card_text(c: Card) -> str:
    d = c.d
    lines = [f"{d.name} ({d.id}) - {d.category.value}, {'/'.join(d.colors)}"]
    stats = []
    if d.category == Category.LEADER:
        stats.append(f"Life {d.life}")
    else:
        stats.append(f"Cost {d.cost}")
    if d.category in (Category.LEADER, Category.CHARACTER):
        stats.append(f"Power {d.power}")
    if d.counter:
        stats.append(f"Counter +{d.counter}")
    lines.append("   " + ", ".join(stats) + (f"  |  Types: {'/'.join(d.types)}" if d.types else ""))
    for t in (d.text, d.trigger_text):
        lines += [f"   {line}" for line in t.split("\n") if line.strip()]
    if d.unimplemented:
        lines.append("   (not implemented in the engine: " + " / ".join(d.unimplemented) + ")")
    return "\n".join(lines)


class HumanAgent(Agent):
    def __init__(self, input_fn: Callable[[str], str] = input, out: Callable[[str], None] = print):
        self.input, self.out = input_fn, out
        self.seen = 0

    def show_new_log(self, g: Game) -> None:
        for line in g.log[self.seen:]:
            self.out("  " + line)
        self.seen = len(g.log)

    def act(self, g: Game, d: Decision) -> Action:
        self.show_new_log(g)
        if d.kind in BOARD_KINDS:
            self.out(render_board(g, d.player))
        self.out(f"\n{d.prompt or d.kind}")
        for i, o in enumerate(d.options, 1):
            self.out(f"  {i}. {o}")
        while True:
            s = self.input("> ").strip().lower()
            if s.isdigit() and 1 <= int(s) <= len(d.options):
                return d.options[int(s) - 1]
            if s in ("b", "board"):
                self.out(render_board(g, d.player))
            elif s == "log":
                self.out("\n".join(g.log))
            elif s.startswith("t ") and s[2:].strip().isdigit() and 1 <= int(s[2:]) <= len(d.options):
                o = d.options[int(s[2:]) - 1]
                uids = [u for u in (o.card, o.target) if u is not None]
                self.out("\n".join(card_text(g.cards[u]) for u in uids) or "That option has no card.")
            elif s in ("q", "quit", "concede"):
                raise Concede
            else:
                self.out(HELP)
