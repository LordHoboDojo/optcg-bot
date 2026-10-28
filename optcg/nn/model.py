"""The policy network: scores each legal option given the visible state.

Cards are embedded by id plus their in-game numbers, mixed with one attention layer so each card
sees the rest of the board, then pooled into a state vector. Each option row is encoded from its
action kind, the acting card and the target, and scored against the state. A value head predicts
the final reward for actor-critic training.
"""
from __future__ import annotations

import torch
from torch import nn
from torch.nn import functional as F

from ..actions import Decision
from ..agents import Agent
from ..engine import Game
from ..env import ACTION_KINDS, CATEGORIES, DECISION_KINDS, KEYWORD_FIELDS, SCALAR_FIELDS, ZONES, card_index, encode

# scale for each scalar so inputs are roughly 0..1
SCALAR_SCALE = torch.tensor([5, 10, 50, 50, 10, 10, 10, 5] * 2 + [1, 20, 8, 1], dtype=torch.float32)
MAX_INDEX = 8  # effect / choice indexes above this share one embedding
CARD_NUMERIC = 1 + (len(ZONES) + 1) + (len(CATEGORIES) + 1) + 7 + len(KEYWORD_FIELDS)


def _card_numeric(cards: torch.Tensor) -> torch.Tensor:
    """[..., CARD_FIELDS] rows -> network inputs (one-hot zone and category, scaled numbers)."""
    mine = cards[..., 1:2]
    zone = F.one_hot(cards[..., 2].long(), len(ZONES) + 1).float()
    cat = F.one_hot(cards[..., 3].long(), len(CATEGORIES) + 1).float()
    power, cost, counter = cards[..., 4:5] / 10, cards[..., 5:6] / 10, cards[..., 6:7] / 2
    rested, don, played, battle = cards[..., 7:8], cards[..., 8:9] / 5, cards[..., 9:10], cards[..., 10:11]
    keywords = cards[..., 11:]
    return torch.cat([mine, zone, cat, power, cost, counter, rested, don, played, battle, keywords], -1)


def collate(obs: list[dict]) -> dict[str, torch.Tensor]:
    """Pad a list of observations from env.encode() into batch tensors."""
    n = len(obs)
    nc = max(len(o["cards"]) for o in obs)
    no = max(len(o["options"]) for o in obs)
    width = len(obs[0]["cards"][0])
    cards = torch.zeros(n, nc, width)
    card_mask = torch.zeros(n, nc, dtype=torch.bool)
    options = torch.full((n, no, 6), -1.0)
    option_mask = torch.zeros(n, no, dtype=torch.bool)
    for i, o in enumerate(obs):
        cards[i, :len(o["cards"])] = torch.tensor(o["cards"])
        card_mask[i, :len(o["cards"])] = True
        options[i, :len(o["options"])] = torch.tensor(o["options"], dtype=torch.float32)
        option_mask[i, :len(o["options"])] = True
    return {"scalars": torch.tensor([o["scalars"] for o in obs], dtype=torch.float32),
            "decision": torch.tensor([o["decision"] for o in obs]),
            "cards": cards, "card_mask": card_mask, "options": options, "option_mask": option_mask}


class PolicyNet(nn.Module):
    def __init__(self, hidden: int = 128, card_dim: int = 64):
        super().__init__()
        self.hidden = hidden
        self.register_buffer("scalar_scale", SCALAR_SCALE.clone(), persistent=False)
        self.card_emb = nn.Embedding(len(card_index()) + 1, card_dim)
        self.card_in = nn.Sequential(nn.Linear(card_dim + CARD_NUMERIC, hidden), nn.ReLU(), nn.Linear(hidden, hidden))
        self.mix = nn.TransformerEncoderLayer(hidden, nhead=4, dim_feedforward=2 * hidden, batch_first=True,
                                              dropout=0.0)
        self.scalars_in = nn.Sequential(nn.Linear(len(SCALAR_FIELDS), hidden), nn.ReLU())
        self.decision_emb = nn.Embedding(len(DECISION_KINDS) + 1, hidden)
        self.state = nn.Sequential(nn.Linear(4 * hidden, hidden), nn.ReLU(), nn.Linear(hidden, hidden))
        self.kind_emb = nn.Embedding(len(ACTION_KINDS) + 1, hidden)
        self.index_emb = nn.Embedding(MAX_INDEX + 2, hidden)
        self.no_card = nn.Parameter(torch.zeros(2, hidden))  # stands in for a missing card / target
        self.score = nn.Sequential(nn.Linear(4 * hidden, hidden), nn.ReLU(), nn.Linear(hidden, 1))
        self.value = nn.Sequential(nn.Linear(hidden, hidden), nn.ReLU(), nn.Linear(hidden, 1))

    def forward(self, b: dict[str, torch.Tensor]) -> tuple[torch.Tensor, torch.Tensor]:
        """Returns (option logits [B, O] with -inf on padding, value [B])."""
        cards, mask = b["cards"], b["card_mask"]
        x = torch.cat([self.card_emb(cards[..., 0].long()), _card_numeric(cards)], -1)
        h = self.mix(self.card_in(x), src_key_padding_mask=~mask)
        m = mask.unsqueeze(-1).float()
        mine = m * cards[..., 1:2]
        theirs = m - mine
        pool_mine = (h * mine).sum(1) / mine.sum(1).clamp(min=1)
        pool_theirs = (h * theirs).sum(1) / theirs.sum(1).clamp(min=1)
        s = torch.cat([pool_mine, pool_theirs, self.scalars_in(b["scalars"] / self.scalar_scale),
                       self.decision_emb(b["decision"])], -1)
        state = self.state(s)

        opts = b["options"]

        def gather(col: int, which: int) -> torch.Tensor:
            idx = opts[..., col].long()
            got = torch.gather(h, 1, idx.clamp(min=0).unsqueeze(-1).expand(-1, -1, self.hidden))
            return torch.where((idx >= 0).unsqueeze(-1), got, self.no_card[which])

        index = opts[..., 3].long().clamp(min=-1, max=MAX_INDEX) + 1
        o = torch.cat([self.kind_emb(opts[..., 0].long().clamp(min=0)) + self.index_emb(index),
                       gather(1, 0), gather(2, 1), state.unsqueeze(1).expand(-1, opts.shape[1], -1)], -1)
        logits = self.score(o).squeeze(-1).masked_fill(~b["option_mask"], float("-inf"))
        return logits, self.value(state).squeeze(-1)


class NeuralAgent(Agent):
    """Plays with a PolicyNet: the best-scored option, or a sample when `sample` is set."""

    def __init__(self, net: PolicyNet, sample: bool = False):
        self.net, self.sample = net.eval(), sample

    @torch.no_grad()
    def act(self, game: Game, d: Decision):
        logits, _ = self.net(collate([encode(game, d)]))
        i = torch.distributions.Categorical(logits=logits[0]).sample() if self.sample else logits[0].argmax()
        return d.options[int(i)]


def save(net: PolicyNet, path: str, **meta) -> None:
    state = {k: v.detach().cpu() for k, v in net.state_dict().items()}
    torch.save({"state": state, "hidden": net.hidden, "cards": card_index(), **meta}, path)


def load(path: str) -> PolicyNet:
    ck = torch.load(path, map_location="cpu", weights_only=False)
    if ck["cards"] != card_index():
        raise ValueError(f"{path} was trained with a different card list; retrain after refreshing card data")
    net = PolicyNet(hidden=ck["hidden"])
    net.load_state_dict(ck["state"])
    return net.eval()
