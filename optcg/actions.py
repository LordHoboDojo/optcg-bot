"""What a player is asked (Decision) and what they answer (Action)."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass(frozen=True)
class Action:
    """One option of a Decision.

    kind values:
      main phase: play, activate, give_don, attack, end
      battle:     block, counter, event, pass
      other:      select, pass, yes, no, first, second, keep, mulligan
    """
    kind: str
    card: Optional[int] = None    # uid of the card acting / being chosen
    target: Optional[int] = None  # uid of the target card
    index: Optional[int] = None   # effect index on the card
    label: str = field(default="", compare=False)

    def __str__(self) -> str:
        return self.label or self.kind


@dataclass(frozen=True)
class Decision:
    player: int
    kind: str      # main, block, counter, trigger, select, confirm, go_first, mulligan
    options: tuple[Action, ...]
    prompt: str = ""
