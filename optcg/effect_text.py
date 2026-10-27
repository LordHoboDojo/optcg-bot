"""Compile printed effect text into engine effects, for the common wordings.

Each line of card text is one effect: leading [tags] (timing, [Once Per Turn],
[DON!! xX], [Your Turn], keywords), an optional cost before a colon, then a
body of sentences. A line is compiled only if every part of it is understood;
otherwise it is reported as unimplemented and the card plays without it.
Hand-written scripts (optcg/scripts.py) cover cards this can't.
"""
from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

from .actions import Action
from .cards import (CANNOT_ATTACK, KEYWORDS, NO_KO_BY_EFFECTS, NO_KO_BY_OPP_EFFECTS, NO_KO_IN_BATTLE,
                    UNBLOCKABLE, Category, Effect, Timing)

NO_KO = {"in battle": NO_KO_IN_BATTLE, "by effects": NO_KO_BY_EFFECTS,
         "by your opponent's effects": NO_KO_BY_OPP_EFFECTS}


class Unsupported(Exception):
    pass


TIMING_TAGS = {
    "On Play": Timing.ON_PLAY,
    "When Attacking": Timing.WHEN_ATTACKING,
    "On K.O.": Timing.ON_KO,
    "On Block": Timing.ON_BLOCK,
    "Activate: Main": Timing.ACTIVATE_MAIN,
    "Main": Timing.MAIN,
    "Counter": Timing.COUNTER,
    "Trigger": Timing.TRIGGER,
    "End of Your Turn": Timing.END_OF_YOUR_TURN,
    "End of Your Opponent's Turn": Timing.END_OF_OPP_TURN,
    "On Your Opponent's Attack": Timing.ON_OPP_ATTACK,
}
PLAYER_CHOSEN = (Timing.ACTIVATE_MAIN, Timing.MAIN, Timing.COUNTER)  # activating is already a choice
CIRCLED = {c: i + 1 for i, c in enumerate("➀➁➂➃➄➅➆➇➈➉")} | {c: i + 1 for i, c in enumerate("①②③④⑤⑥⑦⑧⑨⑩")}


def _self(g, src, card):
    return card is src


@dataclass
class Compiled:
    effects: list[Effect] = field(default_factory=list)
    keywords: set[str] = field(default_factory=set)
    # Permanent effects: (condition(game, src), value, applies_to(game, src, card)).
    static_power: list[tuple] = field(default_factory=list)
    static_keywords: list[tuple] = field(default_factory=list)
    static_cost: list[tuple] = field(default_factory=list)
    hand_cost: list[tuple] = field(default_factory=list)  # (condition(game, card), delta) while in hand
    unimplemented: list[str] = field(default_factory=list)

    def hand_cost_fn(self) -> Optional[Callable]:
        parts = list(self.hand_cost)
        return (lambda g, card: sum(v for cond, v in parts if cond(g, card))) if parts else None

    def static_power_fn(self) -> Optional[Callable]:
        parts = list(self.static_power)
        return (lambda g, src, card: sum(v for cond, v, to in parts if to(g, src, card) and cond(g, src))) \
            if parts else None

    def static_cost_fn(self) -> Optional[Callable]:
        parts = list(self.static_cost)
        return (lambda g, src, card: sum(v for cond, v, to in parts if to(g, src, card) and cond(g, src))) \
            if parts else None

    def static_keywords_fn(self) -> Optional[Callable]:
        parts = list(self.static_keywords)
        return (lambda g, src, card: {kw for cond, kw, to in parts if to(g, src, card) and cond(g, src)}) \
            if parts else None


def normalize(line: str) -> str:
    prev = None
    while prev != line:  # drop explanatory notes (2-8-4), including nested ones
        prev, line = line, re.sub(r"\([^()]*\)", "", line)
    for a, b in (("−", "-"), ("–", "-"), ("－", "-"), ("’", "'"), ("＜", "<"), ("＞", ">"), ("“", '"'), ("”", '"'),
                 ("Charactes", "Characters")):  # a misprint on OP17-076
        line = line.replace(a, b)
    return re.sub(r"\s+", " ", line).strip()


def effect_lines(text: str) -> list[str]:
    """The lines of card text that are effects (not just explanatory notes)."""
    return [l.strip() for l in text.split("\n") if normalize(l)]


def compile_card(effect_text: str, trigger_text: str = "") -> Compiled:
    out = Compiled()
    groups: list[tuple[str, list[str]]] = []  # (raw line, raw bullet lines under it)
    for raw in effect_lines(effect_text + "\n" + trigger_text):
        if raw.startswith("•") and groups:
            groups[-1][1].append(raw)
        else:
            groups.append((raw, []))
    for raw, bullets in groups:
        try:
            _compile_line(normalize(raw), raw, out, [normalize(b.lstrip("•")) for b in bullets])
        except Unsupported:
            out.unimplemented += [raw, *bullets]
    return out


# ---------------------------------------------------------------- line structure

# Costs before the colon. Engine-paid: rest_don, don_minus, rest_self. Paid inside the
# effect: trash (from hand), trash_self, life (Life card to hand).
COST_PARTS = [
    (r"([➀-➉①-⑩])\s*", "rest_don"),
    (r"DON!! -(\d+)\s*", "don_minus"),
    (r"You may rest (\d+) of your DON!! cards?\s*", "rest_don"),
    (r"(?:You may )?rest this (?:Character|Stage|Leader)\s*", "rest_self"),
    (r"(?:and )?this (?:Character|Stage)\s*", "rest_self"),  # "rest 3 of your DON!! cards and this Character"
    (r"(?:You may )?trash this (?:Character|Stage)\s*", "trash_self"),
    (r"(?:You may )?trash (\d+) (.+?) from your hand\s*", "trash"),
    (r"(?:You may )?add (\d+) cards? from the top of your Life cards to your hand\s*", "life"),
    (r"(?:You may )?rest (\d+) of your cards\s*", "rest_cards"),
    (r"(?:You may )?turn (\d+) cards? from the top of your Life cards face-up\s*", "life_face_up"),
    (r"(?:You may )?reveal (\d+) (.+?) from your hand\s*", "reveal"),
    (r"(?:and|,)\s*", None),
]


def _parse_costs(prefix: str) -> dict:
    costs: dict = {}
    while prefix:
        for pat, kind in COST_PARTS:
            cm = re.match(pat, prefix)
            if cm:
                if kind == "rest_don":
                    g1 = cm.group(1)
                    costs[kind] = CIRCLED[g1] if g1 in CIRCLED else int(g1)
                elif kind in ("don_minus", "life", "rest_cards", "life_face_up"):
                    costs[kind] = int(cm.group(1))
                elif kind in ("trash", "reveal"):
                    costs[kind] = (int(cm.group(1)), parse_filter(cm.group(2)))
                elif kind:
                    costs[kind] = True
                prefix = prefix[cm.end():]
                break
        else:
            raise Unsupported
    return costs
CHOOSE_ONE = re.compile(r"(?:(?P<pre>.+?),? then |(?P<pre2>.+?\.) Then, )?(?P<who>Your opponent chooses|[Cc]hoose) one:")


def _compile_line(line: str, raw: str, out: Compiled, bullets: list[str]) -> None:
    tags, alternatives = [], False
    while m := re.match(r"(/)?\[([^\]]+)\]\s*", line):
        alternatives |= bool(m.group(1))
        tags.append(m.group(2))
        line = line[m.end():]
    body = line
    out.keywords |= {t for t in tags if t in KEYWORDS}
    tags = [t for t in tags if t not in KEYWORDS]
    if not tags and not body:
        return

    timings = []
    spec: dict = {}
    for t in tags:
        if t in TIMING_TAGS:
            timings.append(TIMING_TAGS[t])
        elif t == "Once Per Turn":
            spec["once_per_turn"] = True
        elif m := re.fullmatch(r"DON!! x(\d+)", t):
            spec["don_req"] = int(m.group(1))
        elif t == "Your Turn":
            spec["your_turn"] = True
        elif t == "Opponent's Turn":
            spec["opp_turn"] = True
        else:
            raise Unsupported
    if len(timings) > 1 and not alternatives:
        raise Unsupported

    if not timings:
        if bullets:
            raise Unsupported
        _compile_static(body, spec, out)
        return

    costs: dict = {}
    m = re.match(r"^(.*?)\s*:\s*(.*)$", body)
    if m:
        try:  # text before the first colon is a cost only if it reads as one ("[Rush: Character]" isn't)
            costs, body = _parse_costs(m.group(1)), m.group(2)
        except Unsupported:
            pass

    if cm1 := CHOOSE_ONE.fullmatch(body):
        if not bullets:
            raise Unsupported
        action = _choose_one(bullets, opponent=cm1["who"].startswith("Your opponent"))
        pre = cm1["pre"] or cm1["pre2"]
        if pre:
            action = _seq(parse_body(_cap(pre.rstrip(".")) + "."), action)
    elif bullets:
        raise Unsupported
    else:
        action = parse_body(body)

    engine_costs = {k: costs.pop(k) for k in ("rest_don", "don_minus", "rest_self") if k in costs}
    for t in timings:
        ask = t not in PLAYER_CHOSEN
        act, cond = _wrap_costs(action, costs, ask=ask and not engine_costs)
        out.effects.append(Effect(
            t, act, raw, **spec, **engine_costs, condition=cond,
            optional=bool(engine_costs) and ask,
            opt_group=raw if len(timings) > 1 else None))


def _wrap_costs(action, costs: dict, ask: bool):
    """Costs paid while resolving: trash from hand, reveal from hand, rest your cards,
    trash this card, Life card to hand."""
    if not costs:
        return action, None
    trash_n, trash_pred = costs.get("trash", (0, None))
    reveal_n, reveal_pred = costs.get("reveal", (0, None))
    life_n = costs.get("life", 0)
    rest_n = costs.get("rest_cards", 0)
    flip_n = costs.get("life_face_up", 0)
    trash_self = costs.get("trash_self", False)

    def payable(g, src):
        ps = g.players[src.owner]
        if trash_n and sum(1 for c in ps.hand if trash_pred(c)) < trash_n:
            return False
        if reveal_n and sum(1 for c in ps.hand if reveal_pred(c)) < reveal_n:
            return False
        if life_n and len(ps.life) < life_n:
            return False
        if rest_n and g.active_card_count(src.owner) < rest_n:
            return False
        # turning cards face-up that already are does nothing, so it can't pay the cost (1-3-2-1)
        if flip_n and (len(ps.life) < flip_n or any(c.face_up for c in ps.life[-flip_n:])):
            return False
        if trash_self and src.zone not in ("character", "stage"):
            return False
        return True

    def run(g, src):
        if not payable(g, src):
            return
        if ask and not (yield from g.confirm(src.owner, f"Pay the cost of {src.name}'s effect?")):
            return
        ps = g.players[src.owner]
        if trash_n:
            for c in (yield from g.choose_cards(src.owner, [c for c in ps.hand if trash_pred(c)], trash_n,
                                                "Trash for the cost", exactly=True)):
                g.move(c, "trash")
        if reveal_n:
            shown = yield from g.choose_cards(src.owner, [c for c in ps.hand if reveal_pred(c)], reveal_n,
                                              "Reveal for the cost", exactly=True)
            g._log(f"Player {src.owner} reveals {', '.join(c.name for c in shown)}")
        if rest_n:
            yield from g.rest_your_cards(src.owner, rest_n)
        for c in ps.life[-flip_n:] if flip_n else ():
            c.face_up = True
        if life_n:
            for _ in range(life_n):
                g.move(ps.life[-1], "hand")
        if trash_self:
            g.move(src, "trash")
        yield from action(g, src)
    return run, payable


STATIC_TARGETS = [
    (r"This (?:Character|Leader|card) ", lambda m: _self),
    (r"All of your (?:(?P<types>\{[^}]+\}) type )?Characters(?: with a cost of (?P<c>\d+) or more)? ",
     lambda m: lambda g, src, card: card.owner == src.owner and card.zone == "character"
     and (not m["types"] or m["types"][1:-1] in card.d.types)
     and (m["c"] is None or g.cost(card) >= int(m["c"]))),
]


def _compile_static(body: str, spec: dict, out: Compiled) -> None:
    extra = None
    if mc := match_condition(body):
        extra, body = mc[0], _cap(mc[1])

    def cond(g, src, s=spec, extra=extra):
        if s.get("don_req") and src.don < s["don_req"]:
            return False
        if s.get("your_turn") and g.turn_player != src.owner:
            return False
        if s.get("opp_turn") and g.turn_player == src.owner:
            return False
        return extra is None or extra(g, src)

    if re.fullmatch(r"This (?:Character|Leader) cannot attack\.", body):
        out.static_keywords.append((cond, CANNOT_ATTACK, _self))
        return
    if m := re.fullmatch(r"Give this card in your hand -(\d+) cost\.", body):
        out.hand_cost.append((cond, -int(m.group(1))))
        return
    for pat, make in STATIC_TARGETS:
        if m := re.match(pat, body):
            to, rest = make(m), body[m.end():]
            break
    else:
        raise Unsupported
    if km := re.fullmatch(r"cannot be K\.O\.'d (?P<how>in battle|by effects|by your opponent's effects)"
                          r"(?: and gains \[(?P<kw>[^\]]+)\])?\.", rest):
        if km["kw"] and km["kw"] not in KEYWORDS:
            raise Unsupported
        out.static_keywords.append((cond, NO_KO[km["how"]], to))
        if km["kw"]:
            out.static_keywords.append((cond, km["kw"], to))
        return
    # rest is e.g. "gains +3000 power.", "gains [Blocker] and +3000 power.", "gain +12 cost."
    parts = re.fullmatch(r"gains? (?:\[(?P<kw>[^\]]+)\])?(?: and )?(?:\+(?P<v>\d+) (?P<what>power|cost))?\.", rest)
    if not parts or not (parts["kw"] or parts["v"]):
        raise Unsupported
    if parts["kw"]:
        if parts["kw"] not in KEYWORDS:
            raise Unsupported
        out.static_keywords.append((cond, parts["kw"], to))
    if parts["v"]:
        (out.static_power if parts["what"] == "power" else out.static_cost).append((cond, int(parts["v"]), to))


def _choose_one(bullets: list[str], opponent: bool):
    options = [(b, parse_body(_cap(b))) for b in bullets]

    def run(g, src):
        chooser = 1 - src.owner if opponent else src.owner
        opts = [Action("select", index=i, label=text) for i, (text, _) in enumerate(options)]
        a = yield from g.choose(chooser, "choose_one", opts, f"{src.name}: choose one")
        yield from options[a.index][1](g, src)
    return run


# ---------------------------------------------------------------- bodies

def _gen(result):
    if inspect.isgenerator(result):
        yield from result


def _cap(s: str) -> str:
    return s[:1].upper() + s[1:]


def _seq(first, second):
    def both(g, src):
        yield from first(g, src)
        yield from second(g, src)
    return both


def _types_include(card, text: str) -> bool:
    return any(text in t for t in card.d.types)


def _leader(g, src):
    return g.players[src.owner].leader


# Condition clauses, written without "If" and the trailing comma; "If A and B, " combines them.
COND_PARTS = [
    (r"your Leader is \[([^\]]+)\] or has the \{([^}]+)\} type",
     lambda m: lambda g, src: _leader(g, src).name == m.group(1) or m.group(2) in _leader(g, src).d.types),
    (r"your Leader has the \{([^}]+)\} type",
     lambda m: lambda g, src: m.group(1) in _leader(g, src).d.types),
    (r"your Leader's type includes \"([^\"]+)\"",
     lambda m: lambda g, src: _types_include(_leader(g, src), m.group(1))),
    (r"your Leader is \[([^\]]+)\]",
     lambda m: lambda g, src: _leader(g, src).name == m.group(1)),
    (r"your opponent's Leader has (\d+) power or more",
     lambda m: lambda g, src: g.power(g.players[1 - src.owner].leader) >= int(m.group(1))),
    (r"your opponent has (\d+) or (less|more) Life cards?",
     lambda m: lambda g, src: _cmp(len(g.players[1 - src.owner].life), m.group(2), int(m.group(1)))),
    (r"you have (\d+) or (less|more) Life cards?",
     lambda m: lambda g, src: _cmp(len(g.players[src.owner].life), m.group(2), int(m.group(1)))),
    (r"you have (\d+) or (less|more) cards in your hand",
     lambda m: lambda g, src: _cmp(len(g.players[src.owner].hand), m.group(2), int(m.group(1)))),
    (r"you have (\d+) or more DON!! cards on your field",
     lambda m: lambda g, src: g.players[src.owner].don_on_field >= int(m.group(1))),
    (r"there is a Character with a cost of (\d+) or more",
     lambda m: lambda g, src: any(g.cost(c) >= int(m.group(1)) for p in (0, 1) for c in g.characters(p))),
    (r"you have a Character with a cost of (\d+) or more",
     lambda m: lambda g, src: any(g.cost(c) >= int(m.group(1)) for c in g.characters(src.owner))),
    (r"your opponent has a Character with (\d+) power or more",
     lambda m: lambda g, src: any(g.power(c) >= int(m.group(1)) for c in g.characters(1 - src.owner))),
    (r"you have no other \[([^\]]+)\]",
     lambda m: lambda g, src: not any(c is not src and c.name == m.group(1) for c in g.characters(src.owner))),
    (r"you have (\d+) or more Characters with (\d+) base power or more",
     lambda m: lambda g, src: sum(1 for c in g.characters(src.owner) if g.base_power(c) >= int(m.group(2)))
     >= int(m.group(1))),
    (r"you have (\d+) or more Characters with a \[Trigger\]",
     lambda m: lambda g, src: sum(1 for c in g.characters(src.owner) if c.d.trigger_text) >= int(m.group(1))),
    (r"this Character was played on this turn",
     lambda m: lambda g, src: src.played_turn == g.turn_number),
    (r"you have a face-up Life card",
     lambda m: lambda g, src: any(c.face_up for c in g.players[src.owner].life)),
]


def match_condition(text: str):
    """'If A and B, rest' -> (condition, rest), or None if the text doesn't start with a known condition."""
    m = re.match(r"If (.+?), ", text)
    if not m:
        return None
    clauses = m.group(1).split(" and ")
    conds = []
    for clause in clauses:
        for pat, make in COND_PARTS:
            if cm := re.fullmatch(pat, clause):
                conds.append(make(cm))
                break
        else:
            return None
    return (lambda g, src: all(c(g, src) for c in conds)), text[m.end():]


def _cmp(value, direction, n):
    return value <= n if direction == "less" else value >= n


def parse_body(text: str):
    """Return a generator function (game, source) for a body, or raise Unsupported."""
    text = text.strip()
    if not text:
        raise Unsupported
    if mc := match_condition(text):
        cond, inner = mc[0], parse_body(_cap(mc[1]))

        def conditional(g, src, cond=cond, inner=inner):
            if cond(g, src):
                yield from inner(g, src)
        return conditional
    if m := re.fullmatch(r"Your opponent may trash (\d+) cards? from their hand\. If they do not, (.+)", text):
        n, otherwise = int(m.group(1)), parse_body(_cap(m.group(2)))

        def unless_they_trash(g, src):
            o = 1 - src.owner
            if len(g.players[o].hand) >= n and (yield from g.confirm(o, f"Trash {n} cards to stop {src.name}?")):
                yield from g.trash_from_hand(o, n)
                return
            yield from otherwise(g, src)
        return unless_they_trash
    if text.startswith("You may ") and not re.match(r"You may (trash|rest|add) .*:", text):
        inner = parse_body(_cap(text[len("You may "):]))

        def optional(g, src, inner=inner):
            if (yield from g.confirm(src.owner, f"Use {src.name}'s effect?")):
                yield from inner(g, src)
        return optional
    for pat, build in TEMPLATES:
        m = re.match(pat, text)
        if not m:
            continue
        first = build(m)
        rest = text[m.end():].strip()
        if rest.startswith("Then, "):
            rest = rest[len("Then, "):]
        if not rest:
            return first
        return _seq(first, parse_body(_cap(rest)))
    raise Unsupported


# ---------------------------------------------------------------- targets

OPP = (r"(?P<n>\d+) of your opponent's (?P<rested>rested )?Characters"
       r"(?: with (?:a (?P<basec>base )?cost of (?P<cost>\d+)(?P<orless> or less)?"
       r"|(?P<pow>\d+) (?P<basep>base )?power or less))?")


def _opp_pred(m):
    def pred(g, c):
        if m["rested"] and not c.rested:
            return False
        if m["cost"]:
            cost = c.d.cost if m["basec"] else g.cost(c)
            return cost <= int(m["cost"]) if m["orless"] else cost == int(m["cost"])
        if m["pow"]:
            return (c.d.power if m["basep"] else g.power(c)) <= int(m["pow"])
        return True
    return pred


def _opp_targets(g, src, pred):
    return [c for c in g.characters(1 - src.owner) if pred(g, c)]


def parse_filter(f: str) -> Callable:
    """Card filter phrases: 'card', '{Straw Hat Crew} type card other than [Nami]',
    'card with a type including "Whitebeard Pirates"', '[King], [Queen], or [Jack]',
    'Character card with a cost of 2 or less', 'card with a [Trigger]'."""
    f = f.strip()
    if m := re.fullmatch(r"((?:\[[^\]]+\](?:,? or |, )?)+)(?: with a cost of (\d+)(?: or (less|more))?)?", f):
        names = re.findall(r"\[([^\]]+)\]", m.group(1))
        n, d = m.group(2), m.group(3)
        return lambda c: c.name in names and (n is None or _cost_ok(c.d.cost, int(n), d))
    m = re.fullmatch(
        r"(?:(?P<color>red|green|blue|purple|black|yellow) )?"
        r"(?:(?P<types>\{[^}]+\}(?: or \{[^}]+\})*) type )?"
        r"(?:(?P<cat>Character|Event|Stage)(?: cards?)?|cards?)"
        r"(?: with a type including \"(?P<inc>[^\"]+)\")?"
        r"(?P<trig> with a \[Trigger\])?"
        r"(?: with a cost of (?P<c>\d+)(?: or (?P<dir>more|less))?)?"
        r"(?: and a type including \"(?P<inc2>[^\"]+)\")?"
        r"(?: with (?P<pw>\d+) power or less)?"
        r"(?: other than \[(?P<excl>[^\]]+)\])?", f)
    if not m:
        raise Unsupported
    types = re.findall(r"\{([^}]+)\}", m["types"] or "")
    cat = Category(m["cat"]) if m["cat"] else None
    inc = m["inc"] or m["inc2"]

    def pred(c):
        if m["color"] and m["color"] not in c.d.colors:
            return False
        if types and not any(t in c.d.types for t in types):
            return False
        if inc and not _types_include(c, inc):
            return False
        if m["trig"] and not c.d.trigger_text:
            return False
        if cat and c.d.category != cat:
            return False
        if m["c"] is not None and not _cost_ok(c.d.cost, int(m["c"]), m["dir"]):
            return False
        if m["pw"] is not None and c.d.power > int(m["pw"]):
            return False
        if m["excl"] and c.name == m["excl"]:
            return False
        return True
    return pred


def _cost_ok(cost, n, direction):
    return cost >= n if direction == "more" else cost <= n if direction == "less" else cost == n


# ---------------------------------------------------------------- templates

def _simple(fn):
    def run(g, src):
        fn(g, src)
        yield from ()
    return run


def t_ko(m):
    pred, n = _opp_pred(m), int(m["n"])

    def run(g, src):
        for c in (yield from g.choose_cards(src.owner, _opp_targets(g, src, pred), n, "K.O. a Character")):
            yield from g.ko(c, by=src.owner)
    return run


def t_ko_total(m):
    budget = int(m["t"])

    def run(g, src):
        left, chosen = budget, []
        while True:
            cands = [c for c in g.characters(1 - src.owner) if c not in chosen and g.cost(c) <= left]
            pick = yield from g.choose_cards(src.owner, cands, 1, f"K.O. Characters with total cost {budget} or less")
            if not pick:
                break
            chosen.append(pick[0])
            left -= g.cost(pick[0])
        for c in chosen:
            yield from g.ko(c, by=src.owner)
    return run


def t_bottom_all(m):
    pred = _opp_pred(m)

    def run(g, src):
        for c in _opp_targets(g, src, pred):
            yield from g.remove_from_field(c, "deck", by=src.owner, bottom=True)
    return run


def t_rest_leader_or_char(m):
    n = int(m["n"])

    def run(g, src):
        for c in (yield from g.choose_cards(src.owner, g.leader_and_characters(1 - src.owner), n, "Rest a card")):
            g.rest(c)
    return run


def t_no_character_play(m):
    return _simple(lambda g, src: g.forbid_character_play(src.owner))


def t_set_active(m):
    n, cands = int(m["n"]), _own_candidates(m["who"])

    def run(g, src):
        rested = [c for c in cands(g, src.owner) if c.rested]
        for c in (yield from g.choose_cards(src.owner, rested, n, "Set as active")):
            g.set_active(c)
    return run


def t_self_gain(m):
    kw, v = m["kw"], int(m["v"]) if m["v"] else 0
    until = {"battle": "battle", "turn": "turn"}.get(m["dur"], "next_end")
    if kw and kw not in KEYWORDS or not (kw or v):
        raise Unsupported

    def fn(g, src):
        if kw:
            g.grant_keyword(src, kw, until, by=src.owner)
        if v:
            g.add_power(src, v, until, by=src.owner)
    return _simple(fn)


def t_rest(m):
    pred, n = _opp_pred(m), int(m["n"])

    def run(g, src):
        for c in (yield from g.choose_cards(src.owner, _opp_targets(g, src, pred), n, "Rest a Character")):
            g.rest(c)
    return run


def t_rest_all(m):
    def fn(g, src):
        for c in g.characters(1 - src.owner):
            g.rest(c)
    return _simple(fn)


def t_cannot_attack(m):
    pred, n = _opp_pred(m), int(m["n"])

    def run(g, src):
        for c in (yield from g.choose_cards(src.owner, _opp_targets(g, src, pred), n, "Character can't attack")):
            g.grant_keyword(c, CANNOT_ATTACK, "next_end", by=src.owner)
    return run


def t_ko_stage(m):
    n = int(m["n"])

    def run(g, src):
        stage = g.players[1 - src.owner].stage
        for c in (yield from g.choose_cards(src.owner, [stage] if stage else [], n, "K.O. a Stage")):
            g.move(c, "trash")
    return run


def t_debuff(m):
    n, v, kind = int(m["n"]), int(m["v"]), m["what"]
    with_leader, rested = m["who"].startswith("Leader"), bool(m["rested"])

    def run(g, src):
        o = 1 - src.owner
        cands = g.leader_and_characters(o) if with_leader else g.characters(o)
        cands = [c for c in cands if c.rested or not rested]
        for c in (yield from g.choose_cards(src.owner, cands, n, f"Give -{v} {kind}")):
            (g.add_power if kind == "power" else g.add_cost)(c, -v, "turn", by=src.owner)
    return run


def t_debuff_all(m):
    v, kind = int(m["v"]), m["what"]

    def fn(g, src):
        for c in g.characters(1 - src.owner):
            (g.add_power if kind == "power" else g.add_cost)(c, -v, "turn", by=src.owner)
    return _simple(fn)


def t_bounce(m):
    n, opp_only, where = int(m["n"]), bool(m["opp"]), m["where"]
    cost = int(m["cost"]) if m["cost"] else None
    trig = bool(m.groupdict().get("trig"))

    def run(g, src):
        sides = [1 - src.owner] if opp_only else [1 - src.owner, src.owner]
        cands = [c for p in sides for c in g.characters(p)
                 if (cost is None or g.cost(c) <= cost) and (not trig or c.d.trigger_text)]
        for c in (yield from g.choose_cards(src.owner, cands, n, "Choose a Character to remove")):
            yield from g.remove_from_field(c, where, by=src.owner, bottom=where == "deck")
    return run


def t_draw_trash(m):
    d, t = int(m["d"]), int(m["t"])

    def run(g, src):
        g.draw(src.owner, d)
        yield from g.trash_from_hand(src.owner, t)
    return run


def t_draw(m):
    return _simple(lambda g, src, n=int(m["d"]): g.draw(src.owner, n))


def t_trash_hand(m):
    n = int(m["t"])

    def run(g, src):
        yield from g.trash_from_hand(src.owner, n)
    return run


def t_opp_trashes(m):
    n = int(m["n"])

    def run(g, src):
        yield from g.trash_from_hand(1 - src.owner, n)
    return run


def t_trash_opp_hand(m):
    """You can't see the cards, so the choice is effectively random (8-4-4-2)."""
    n = int(m["n"])

    def fn(g, src):
        hand = g.players[1 - src.owner].hand
        for c in g.rng.sample(hand, min(n, len(hand))):
            g.move(c, "trash")
    return _simple(fn)


def t_opp_bottoms(m):
    n = int(m["n"])

    def run(g, src):
        o = 1 - src.owner
        chosen = yield from g.choose_cards(o, g.players[o].hand, n, "Place on the bottom of your deck", exactly=True)
        yield from g.place_in_order(o, chosen, "deck", bottom=True)
    return run


def t_mill(m):
    n = int(m["t"])

    def fn(g, src):
        deck = g.players[src.owner].deck
        for _ in range(min(n, len(deck))):
            g.move(deck[-1], "trash")
    return _simple(fn)


def t_life_to_hand(m):
    n, opp = int(m["n"]), bool(m["opp"])

    def fn(g, src):
        life = g.players[1 - src.owner if opp else src.owner].life
        for _ in range(min(n, len(life))):
            g.move(life[-1], "hand")
    return _simple(fn)


def t_deck_to_life(m):
    n = int(m["n"])

    def run(g, src):
        deck = g.players[src.owner].deck
        for _ in range(min(n, len(deck))):
            if not (yield from g.confirm(src.owner, "Add the top card of your deck to your Life?")):
                return
            g.move(deck[-1], "life")
    return run


def t_add_don(m):
    n, rested = int(m["n"]), m["how"].startswith("rest")
    return _simple(lambda g, src: g.add_don(src.owner, n, rested=rested))


def t_set_don_active(m):
    n = int(m["n"])

    def fn(g, src):
        ps = g.players[src.owner]
        k = min(n, ps.don_rested)
        ps.don_rested -= k
        ps.don_active += k
    return _simple(fn)


def t_give_rested_don(m):
    n, chars, name = int(m["n"]), bool(m["or"]), m["name"]

    def run(g, src):
        cands = g.leader_and_characters(src.owner) if chars else [g.players[src.owner].leader]
        cands = [c for c in cands if name is None or c.name == name]
        if not g.players[src.owner].don_rested:
            return
        for c in (yield from g.choose_cards(src.owner, cands, 1, "Give rested DON!!")):
            g.give_don(src.owner, c, n, from_rested=True)
    return run


OWN_PART = re.compile(
    r"(?P<base>Leader or Character cards|Leader or Characters|Characters or \[[^\]]+\]|Leader|Characters?"
    r"|(?P<types>\{[^}]+\}(?: or \{[^}]+\})*) type Characters?|\[(?P<name>[^\]]+)\](?: cards?)?)"
    r"(?: with a type including \"(?P<inc>[^\"]+)\")?")


def _own_candidates(who: str):
    """Which of your cards a phrase allows: 'Leader or Characters', '[Nami] cards',
    '{Land of Wano} type Characters or up to 1 of your Characters with a type including "X"'."""
    parts = []
    for text in re.split(r" or up to \d+ of your ", who):
        m = OWN_PART.fullmatch(text)
        if not m:
            raise Unsupported
        parts.append(m)

    def part_cands(g, p, m):
        base = m["base"]
        if base == "Leader":
            pool = [g.players[p].leader]
        elif base.startswith("Character") and not base.startswith("Characters or") or m["types"]:
            pool = g.characters(p)
        else:
            pool = g.leader_and_characters(p)
        types = re.findall(r"\{([^}]+)\}", m["types"] or "")
        orname = re.search(r"Characters or \[([^\]]+)\]", base)
        out = []
        for c in pool:
            if types and not any(t in c.d.types for t in types):
                continue
            if m["name"] and c.name != m["name"]:
                continue
            if orname and not (c.category == Category.CHARACTER or c.name == orname.group(1)):
                continue
            if m["inc"] and not _types_include(c, m["inc"]):
                continue
            out.append(c)
        return out

    def cands(g, p):
        out = []
        for m in parts:
            out += [c for c in part_cands(g, p, m) if c not in out]
        return out
    return cands


WHO = r"(?P<who>.+?)"  # checked by _own_candidates


def t_buff(m):
    n, v, dur, cands = int(m["n"]), int(m["v"]), m["dur"], _own_candidates(m["who"])

    def run(g, src):
        for c in (yield from g.choose_cards(src.owner, cands(g, src.owner), n, f"+{v} power")):
            g.add_power(c, v, dur, by=src.owner)
    return run


def t_grant(m):
    n, kw, cands = int(m["n"]), m["kw"], _own_candidates(m["who"])
    if kw not in KEYWORDS:
        raise Unsupported

    def run(g, src):
        for c in (yield from g.choose_cards(src.owner, cands(g, src.owner), n, f"Gains [{kw}]")):
            g.grant_keyword(c, kw, "turn", by=src.owner)
    return run


def t_leader_buff(m):
    v, dur = int(m["v"]), m["dur"]
    return _simple(lambda g, src: g.add_power(g.players[src.owner].leader, v, dur, by=src.owner))


def t_leader_keyword(m):
    kw, name = m["kw"], m["name"]
    if kw not in KEYWORDS:
        raise Unsupported

    def fn(g, src):
        leader = g.players[src.owner].leader
        if name is None or leader.name == name:
            g.grant_keyword(leader, kw, "turn", by=src.owner)
    return _simple(fn)


def t_leader_base_power(m):
    v, who = int(m["v"]), m["who"] or ""

    def fn(g, src):
        leader = g.players[src.owner].leader
        if who.startswith("[") and leader.name != who[1:-1]:
            return
        if who.startswith("{") and who[1:who.index("}")] not in leader.d.types:
            return
        if who == "monocolored" and len(leader.d.colors) != 1:
            return
        g.set_base_power(leader, v, "next_end", by=src.owner)
    return _simple(fn)


def t_unblockable(m):
    return _simple(lambda g, src: g.grant_keyword(src, UNBLOCKABLE, "battle", by=src.owner))


def t_play_self(m):
    def run(g, src):
        if src.category in (Category.CHARACTER, Category.STAGE):
            yield from g.play_card(src)
    return run


def t_activate_own(m):
    if m["t"] not in TIMING_TAGS:
        raise Unsupported
    timing = TIMING_TAGS[m["t"]]

    def run(g, src):
        for i in src.d.effect_indexes(timing)[:1]:
            yield from _gen(src.d.effects[i].resolve(g, src))
    return run


def t_look(m):
    n, k, pred, to_trash = int(m["n"]), int(m["k"]), parse_filter(m["f"]), m["rest"].startswith("trash")

    def run(g, src):
        deck = g.players[src.owner].deck
        top = deck[-n:][::-1]
        picks = yield from g.choose_cards(src.owner, [c for c in top if pred(c)], k, "Add to hand")
        for c in picks:
            g.move(c, "hand")
        rest = [c for c in top if c not in picks]
        if to_trash:
            for c in rest:
                g.move(c, "trash")
        else:
            yield from g.place_in_order(src.owner, rest, "deck", bottom=True)
    return run


def t_scry(m):
    n = int(m["n"])

    def run(g, src):
        deck = g.players[src.owner].deck
        for c in deck[-n:][::-1]:
            if (yield from g.confirm(src.owner, f"Put {c.name} on the bottom of your deck?")):
                g.move(c, "deck", bottom=True)
    return run


def t_play_from(m):
    n, zones, rested = int(m["n"]), m["zone"].split(" or "), bool(m["rested"])
    pred = parse_filter(m["f"])

    def run(g, src):
        ps = g.players[src.owner]
        pool = [c for z in zones for c in (ps.hand if z == "hand" else ps.trash)]
        cands = [c for c in pool if c.category in (Category.CHARACTER, Category.STAGE) and pred(c)]
        for c in (yield from g.choose_cards(src.owner, cands, n, "Play a card")):
            yield from g.play_card(c, rested=rested)
    return run


def t_char_to_life(m):
    """A Character goes from the field to the top or bottom of its owner's Life face-up."""
    n, opp_only, cost = int(m["n"]), bool(m["opp"]), int(m["cost"])

    def run(g, src):
        sides = [1 - src.owner] if opp_only else [1 - src.owner, src.owner]
        cands = [c for p in sides for c in g.characters(p) if g.cost(c) <= cost]
        for c in (yield from g.choose_cards(src.owner, cands, n, "Add to Life face-up")):
            bottom = yield from g.confirm(src.owner, f"Put {c.name} at the bottom of the Life cards?")
            yield from g.remove_from_field(c, "life", by=src.owner, bottom=bottom, face_up=True)
    return run


def t_hand_to_life(m):
    n, pred = int(m["n"]), parse_filter(m["f"])

    def run(g, src):
        hand = g.players[src.owner].hand
        for c in (yield from g.choose_cards(src.owner, [c for c in hand if pred(c)], n, "Add to Life face-up")):
            g.move(c, "life", face_up=True)
    return run


def t_trash_to_hand(m):
    n, pred = int(m["n"]), parse_filter(m["f"])

    def run(g, src):
        trash = g.players[src.owner].trash
        for c in (yield from g.choose_cards(src.owner, [c for c in trash if pred(c)], n, "Add to hand from trash")):
            g.move(c, "hand")
    return run


TEMPLATES: list[tuple[str, Callable]] = [
    (rf"K\.O\. up to {OPP}\.", t_ko),
    (r"K\.O\. up to (?P<n>\d+) of your opponent's Stages\.", t_ko_stage),
    (rf"Rest up to {OPP}\.", t_rest),
    (r"Rest all of your opponent's Characters\.", t_rest_all),
    (rf"Up to {OPP} cannot attack until the end of your opponent's next End Phase\.", t_cannot_attack),
    (r"Give up to (?P<n>\d+) of your opponent's (?P<rested>rested )?(?P<who>Leader or Character cards|"
     r"Leader or Characters|Characters) -(?P<v>\d+) (?P<what>power|cost) during this turn\.", t_debuff),
    (r"Give all of your opponent's Characters -(?P<v>\d+) (?P<what>power|cost) during this turn\.", t_debuff_all),
    (r"Return up to (?P<n>\d+) (?P<opp>of your opponent's )?Characters?"
     r"(?: with a cost of (?P<cost>\d+) or less| with a \[Trigger\](?P<trig>))? to the owner's (?P<where>hand)\.",
     t_bounce),
    (r"K\.O\. your opponent's Characters with a total cost of (?P<t>\d+) or less\.", t_ko_total),
    (r"Place all of your opponent's (?P<n>)(?P<rested>)Characters with a (?P<basec>base )?cost of (?P<cost>\d+)"
     r"(?P<orless> or less)?(?P<pow>)(?P<basep>) at the bottom of the owner's deck"
     r"(?: in any order(?: of the owner's choosing)?)?\.", t_bottom_all),
    (r"Rest up to (?P<n>\d+) of your opponent's Leader or Characters\.", t_rest_leader_or_char),
    (r"You cannot play Character cards during this turn\.", t_no_character_play),
    (r"Look at (?P<n>\d+) cards? from the top of your deck, reorganize them in any order and place them at "
     r"the top or bottom of your deck\.", t_scry),
    (r"Place up to (?P<n>\d+) (?P<opp>of your opponent's )?Characters? with a cost of (?P<cost>\d+) or less "
     r"at the bottom of the owner's (?P<where>deck)\.", t_bounce),
    (r"Draw (?P<d>\d+) cards? and trash (?P<t>\d+) cards? from your hand\.", t_draw_trash),
    (r"Draw (?P<d>\d+) cards?(?:\.|,? and )", t_draw),
    (r"Trash (?P<t>\d+) cards? from your hand\.", t_trash_hand),
    (r"Your opponent trashes (?P<n>\d+) cards? from their hand\.", t_opp_trashes),
    (r"Trash (?P<n>\d+) cards? from your opponent's hand\.", t_trash_opp_hand),
    (r"Your opponent places (?P<n>\d+) cards? from their hand at the bottom of their deck(?: in any order)?\.",
     t_opp_bottoms),
    (r"Trash (?P<t>\d+) cards? from the top of your deck\.", t_mill),
    (r"Add (?:up to )?(?P<n>\d+) cards? from the top of your (?P<opp>opponent's )?Life cards to "
     r"(?:your|the owner's) hand\.", t_life_to_hand),
    (r"Add up to (?P<n>\d+) cards? from the top of your deck to the top of your Life cards\.", t_deck_to_life),
    (r"Add up to (?P<n>\d+) DON!! cards? from your DON!! deck and (?P<how>set (?:it|them) as active|rest (?:it|them))\.",
     t_add_don),
    (r"Add up to (?P<n>\d+) DON!! cards? as (?P<how>active|rested) from your DON!! deck\.", t_add_don),
    (r"Set up to (?P<n>\d+) of your DON!! cards as active\.", t_set_don_active),
    (r"Give up to (?P<n>\d+) rested DON!! cards? to your (?:\[(?P<name>[^\]]+)\] )?Leader"
     r"(?P<or> or \d+ of your Characters)?\.", t_give_rested_don),
    (rf"Set up to (?P<n>\d+) of your {WHO} as active\.", t_set_active),
    (rf"Up to (?P<n>\d+) of your {WHO} gains? \+(?P<v>\d+) power during this (?P<dur>battle|turn)\.", t_buff),
    (rf"Up to (?P<n>\d+) of your {WHO} gains? \[(?P<kw>[^\]]+)\] during this turn\.", t_grant),
    (r"Your Leader gains \+(?P<v>\d+) power during this (?P<dur>battle|turn)\.", t_leader_buff),
    (r"Your (?:\[(?P<name>[^\]]+)\] )?Leader gains \[(?P<kw>[^\]]+)\] during this turn\.", t_leader_keyword),
    (r"Your (?:(?P<who>\[[^\]]+\]|\{[^}]+\} type|monocolored) )?Leader's base power becomes (?P<v>\d+) "
     r"until the end of your opponent's next End Phase\.", t_leader_base_power),
    (r"This (?:Character|Leader|card) gains (?:\[(?P<kw>[^\]]+)\])?(?: and )?(?:\+(?P<v>\d+) power)? "
     r"(?:during this (?P<dur>battle|turn)|until the end of your opponent's next End Phase)\.", t_self_gain),
    (r"Your opponent cannot activate \[Blocker\] during this battle\.", t_unblockable),
    (r"Play this (?:Character )?card(?: from your trash)?\.", t_play_self),
    (r"Activate this card's \[(?P<t>[^\]]+)\] effect\.", t_activate_own),
    (r"Look at (?P<n>\d+) cards? from the top of your deck and place them at the top or bottom of the deck "
     r"in any order\.", t_scry),
    (r"Look at (?P<n>\d+) cards? from the top of your deck; reveal up to (?P<k>\d+) (?P<f>.+?) and add (?:it|them) "
     r"to your hand\. Then, (?P<rest>place the rest at the bottom of your deck in any order|trash the rest)\.", t_look),
    (r"Play up to (?P<n>\d+) (?P<f>[^.]+?) from your (?P<zone>hand or trash|hand|trash)(?P<rested> rested)?\.",
     t_play_from),
    (r"Add up to (?P<n>\d+) (?P<f>[^.]+?) from your trash to your hand\.", t_trash_to_hand),
    (r"(?:Add|Place) up to (?P<n>\d+) (?P<opp>of your opponent's )?Characters? with a cost of (?P<cost>\d+) or less "
     r"(?:to|at) the top or bottom of (?:the owner's|your opponent's|their) Life cards face-up\.", t_char_to_life),
    (r"Add up to (?P<n>\d+) (?P<f>[^.]+?) from your hand to the top of your Life cards face-up\.", t_hand_to_life),
]
