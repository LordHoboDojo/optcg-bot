"""Deck construction rules (5-1)."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from .cards import CardDef, Category

DECK_SIZE = 50
MAX_COPIES = 4


class DeckError(ValueError):
    pass


@dataclass
class Deck:
    leader: CardDef
    cards: list[CardDef]

    @classmethod
    def from_counts(cls, leader: CardDef, counts: dict[CardDef, int]) -> "Deck":
        return cls(leader, [d for d, n in counts.items() for _ in range(n)])

    def problems(self) -> list[str]:
        out = []
        if self.leader.category != Category.LEADER:
            out.append(f"{self.leader.id} is not a Leader")
        if len(self.cards) != DECK_SIZE:
            out.append(f"deck has {len(self.cards)} cards, needs {DECK_SIZE}")
        allowed = set(self.leader.colors)
        for d in {d.id: d for d in self.cards}.values():
            if d.category == Category.LEADER:
                out.append(f"{d.id} is a Leader and cannot be in the deck")
            if not set(d.colors) & allowed:
                out.append(f"{d.id} has no color shared with the Leader")
        unlimited = {d.id for d in self.cards if "any number of this card in your deck" in d.text}
        for card_id, n in Counter(d.id for d in self.cards).items():
            if n > MAX_COPIES and card_id not in unlimited:
                out.append(f"{n} copies of {card_id} (max {MAX_COPIES})")
        return out

    def validate(self) -> None:
        problems = self.problems()
        if problems:
            raise DeckError("; ".join(problems))
