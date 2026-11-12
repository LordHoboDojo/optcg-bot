"""The policy network: scores each legal option given the visible state.

Cards are embedded by id plus their in-game numbers, mixed with one attention layer so each card
sees the rest of the board, then pooled into a state vector. Each option row is encoded from its
action kind, the acting card and the target, and scored against the state. A value head predicts
the final reward for actor-critic training.

A conditioned network (`PolicyNet(..., card_feat=card_features(), deck_cond=True)`, what
`train.py --decks` builds) also reads, for every card, fixed features of what the card is and what
its compiled effects do (`card_features`), added to its id embedding so rare cards share what the
network learns from common ones; and it reads the deciding player's own decklist (the mean of its
cards' vectors) and the opponent's Leader, so one network can play many decks. Never the
opponent's list or hand: env.encode() gives only what the player knows.
"""
from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

import torch
from torch import nn
from torch.nn import functional as F

from ..actions import Decision
from ..agents import Agent
from ..engine import Game
from ..env import ACTION_KINDS, CATEGORIES, DECISION_KINDS, KEYWORD_FIELDS, SCALAR_FIELDS, ZONES, card_index, encode

COLORS = ("red", "green", "blue", "purple", "black", "yellow")
MIN_TOKEN_CARDS = 2  # effect tokens on fewer cards than this share one "other" column per kind

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


def _token_vocab(tokens: dict[str, list[str]]) -> list[str]:
    seen: dict[str, int] = {}
    for ts in tokens.values():
        for t in set(ts):
            seen[t] = seen.get(t, 0) + 1
    common = sorted(t for t, n in seen.items() if n >= MIN_TOKEN_CARDS)
    return common + sorted({_other(t) for t in seen if seen[t] < MIN_TOKEN_CARDS})


def _other(token: str) -> str:
    return token.split(":")[0] + ":other" if ":" in token else "other"


def card_features() -> torch.Tensor:
    """[len(card_index()) + 1, F] fixed features per card (row 0: no card): category, colors, cost,
    power, counter, Life, a [Trigger], and how often each effect token of the Rust compiler's
    `card_tokens` (timings, costs, keywords, action kinds such as "a:ko" or "a:draw@opp") comes
    up, capped at 2. Without the Rust engine the effect tokens are left out.

    A network keeps the table it was built with in its weights, so new cards or compiler
    changes don't shift what a trained network reads."""
    return _card_feature_table()[1]


def card_feature_names() -> list[str]:
    """The column names of card_features()."""
    return _card_feature_table()[0]


@lru_cache(maxsize=1)
def _card_feature_table() -> tuple[list[str], torch.Tensor]:
    from ..env import rust_available, rust_db
    raw = {c["id"]: c for c in json.loads((Path(__file__).resolve().parents[2] / "data" / "cards.json").read_text())}
    ids = card_index()
    tokens: dict[str, list[str]] = {}
    if rust_available():
        db = rust_db()
        for cid in ids:
            if cid in raw:
                tokens[cid] = db.card_tokens(cid)
    vocab = _token_vocab(tokens)
    col = {t: i for i, t in enumerate(vocab)}
    cats = [c.value for c in CATEGORIES]
    base = len(cats) + len(COLORS) + 6
    out = torch.zeros(len(ids) + 1, base + len(vocab))
    for cid, row in ids.items():
        j = raw.get(cid)
        if j is None:
            continue
        f = out[row]
        if j["category"] in cats:
            f[cats.index(j["category"])] = 1
        for c in j.get("colors") or ():
            if c in COLORS:
                f[len(cats) + COLORS.index(c)] = 1
        k = len(cats) + len(COLORS)
        f[k:k + 6] = torch.tensor([(j.get("cost") or 0) / 10, (j.get("power") or 0) / 10000,
                                   (j.get("counter") or 0) / 2000, (j.get("life") or 0) / 5,
                                   float(bool(j.get("trigger"))), 1.0])
        for t in tokens.get(cid, ()):
            i = base + col[t if t in col else _other(t)]
            f[i] = min(f[i] + 0.5, 1.0)
    names = cats + list(COLORS) + ["cost", "power", "counter", "life", "has_trigger", "known"] + vocab
    return names, out


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
    out = {"scalars": torch.tensor([o["scalars"] for o in obs], dtype=torch.float32),
           "decision": torch.tensor([o["decision"] for o in obs]),
           "cards": cards, "card_mask": card_mask, "options": options, "option_mask": option_mask}
    if all("deck" in o for o in obs):
        nd = max(len(o["deck"]) for o in obs)
        deck = torch.zeros(n, nd, dtype=torch.long)
        for i, o in enumerate(obs):
            deck[i, :len(o["deck"])] = torch.tensor(o["deck"])
        out["deck"] = deck  # 0 pads: no card
        out["opp_leader"] = torch.tensor([o["opp_leader"] for o in obs])
    return out


class PolicyNet(nn.Module):
    """`card_feat` (a card_features() table, or an int: its width, for loading weights) adds the
    per-card features; `deck_cond` the decklist and opponent's Leader. Both off is the original
    network, so older checkpoints still load."""

    def __init__(self, hidden: int = 128, card_dim: int = 64, card_feat: torch.Tensor | int | None = None,
                 deck_cond: bool = False):
        super().__init__()
        self.hidden, self.card_dim, self.deck_cond = hidden, card_dim, deck_cond
        self.register_buffer("scalar_scale", SCALAR_SCALE.clone(), persistent=False)
        self.card_emb = nn.Embedding(len(card_index()) + 1, card_dim)
        self.n_feat = 0
        if card_feat is not None:
            table = torch.zeros(len(card_index()) + 1, card_feat) if isinstance(card_feat, int) else card_feat.clone()
            self.n_feat = table.shape[1]
            self.register_buffer("card_feat", table)  # saved with the weights
            self.feat_in = nn.Linear(self.n_feat, card_dim)
        if deck_cond:
            self.deck_in = nn.Sequential(nn.Linear(2 * card_dim, hidden), nn.ReLU(), nn.Linear(hidden, hidden))
            self.opp_leader_in = nn.Sequential(nn.Linear(card_dim, hidden), nn.ReLU())
        self.card_in = nn.Sequential(nn.Linear(card_dim + CARD_NUMERIC, hidden), nn.ReLU(), nn.Linear(hidden, hidden))
        self.mix = nn.TransformerEncoderLayer(hidden, nhead=4, dim_feedforward=2 * hidden, batch_first=True,
                                              dropout=0.0)
        self.scalars_in = nn.Sequential(nn.Linear(len(SCALAR_FIELDS), hidden), nn.ReLU())
        self.decision_emb = nn.Embedding(len(DECISION_KINDS) + 1, hidden)
        self.state = nn.Sequential(nn.Linear((6 if deck_cond else 4) * hidden, hidden), nn.ReLU(),
                                   nn.Linear(hidden, hidden))
        self.kind_emb = nn.Embedding(len(ACTION_KINDS) + 1, hidden)
        self.index_emb = nn.Embedding(MAX_INDEX + 2, hidden)
        self.no_card = nn.Parameter(torch.zeros(2, hidden))  # stands in for a missing card / target
        self.score = nn.Sequential(nn.Linear(4 * hidden, hidden), nn.ReLU(), nn.Linear(hidden, 1))
        self.value = nn.Sequential(nn.Linear(hidden, hidden), nn.ReLU(), nn.Linear(hidden, 1))

    def config(self) -> dict:
        """What PolicyNet(**config) needs to rebuild this network before loading its weights."""
        return dict(hidden=self.hidden, card_dim=self.card_dim, card_feat=self.n_feat or None,
                    deck_cond=self.deck_cond)

    def card_vec(self, ids: torch.Tensor) -> torch.Tensor:
        """[...] card ids -> [..., card_dim]: the id embedding plus, if on, the card's features."""
        v = self.card_emb(ids)
        if self.n_feat:
            v = v + self.feat_in(self.card_feat[ids])
        return v

    def forward(self, b: dict[str, torch.Tensor]) -> tuple[torch.Tensor, torch.Tensor]:
        """Returns (option logits [B, O] with -inf on padding, value [B])."""
        cards, mask = b["cards"], b["card_mask"]
        x = torch.cat([self.card_vec(cards[..., 0].long()), _card_numeric(cards)], -1)
        h = self.mix(self.card_in(x), src_key_padding_mask=~mask)
        m = mask.unsqueeze(-1).float()
        mine = m * cards[..., 1:2]
        theirs = m - mine
        pool_mine = (h * mine).sum(1) / mine.sum(1).clamp(min=1)
        pool_theirs = (h * theirs).sum(1) / theirs.sum(1).clamp(min=1)
        parts = [pool_mine, pool_theirs, self.scalars_in(b["scalars"] / self.scalar_scale),
                 self.decision_emb(b["decision"])]
        if self.deck_cond:
            if "deck" not in b:
                raise ValueError("this network reads the decklist: observations need 'deck' and 'opp_leader'")
            deck = b["deck"]
            vecs = self.card_vec(deck)
            in_deck = (deck > 0).unsqueeze(-1).float()
            in_deck[:, 0] = 0  # the Leader goes in on its own
            mean = (vecs * in_deck).sum(1) / in_deck.sum(1).clamp(min=1)
            parts += [self.deck_in(torch.cat([vecs[:, 0], mean], -1)), self.opp_leader_in(self.card_vec(b["opp_leader"]))]
        s = torch.cat(parts, -1)
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
        return d.options[self._choose(encode(game, d))]

    @torch.no_grad()
    def pick(self, game) -> int:
        from ..env import encode_rust
        return self._choose(encode_rust(game))

    def _choose(self, obs: dict) -> int:
        logits, _ = self.net(collate([obs]))
        i = torch.distributions.Categorical(logits=logits[0]).sample() if self.sample else logits[0].argmax()
        return int(i)


def conditioned(hidden: int = 128) -> PolicyNet:
    """A new network with card features and deck conditioning (what --decks trains)."""
    return PolicyNet(hidden, card_feat=card_features(), deck_cond=True)


def from_state(state: dict, config: dict | int) -> PolicyNet:
    """A network from weights and its config() (or, for the original network, its hidden size)."""
    net = PolicyNet(**config) if isinstance(config, dict) else PolicyNet(config)
    net.load_state_dict(state)
    return net.eval()


def save(net: PolicyNet, path: str, **meta) -> None:
    state = {k: v.detach().cpu() for k, v in net.state_dict().items()}
    torch.save({"state": state, "hidden": net.hidden, "config": net.config(), "cards": card_index(), **meta}, path)


def load(path: str) -> PolicyNet:
    ck = torch.load(path, map_location="cpu", weights_only=False)
    if ck["cards"] != card_index():
        raise ValueError(f"{path} was trained with a different card list; retrain after refreshing card data")
    return from_state(ck["state"], ck.get("config", ck["hidden"]))
