# optcg-bot

A ONE PIECE CARD GAME rules engine and bots, written from the official
Comprehensive Rules v1.2.1 (`rules/`). Pure Python 3.10, no dependencies.

```bash
# play Rocks yourself against the Linlin bot (run from this folder)
python3 -m optcg.play --deck0 "(Blue) Rocks.D.Xebec" --deck1 "(Yellow) Charlotte Linlin" --human 0 --p1 greedy
python3 -m optcg.play --games 200 --p0 greedy --p1 random   # bot vs bot win rates
python3 -m optcg.play --games 1 --log                        # print one game's log
python3 -m unittest discover tests                           # rule tests (.venv/bin/python also runs the PyTorch ones)
```

## Watching a game

`optcg.replay` records a game and writes it as one HTML file you open in a browser: a slideshow
with one step per decision, showing both boards and hands, what happened since the last step,
the options and the one chosen. Click a card for its text; arrow keys step, space plays.

```bash
python3 -m optcg.replay --p0 greedy --p1 greedy --seed 3 --open
.venv/bin/python -m optcg.replay --p0 runs/ppo/latest.pt --p1 greedy --seed 3 --out replays/net.html --open
```

Decks default to Rocks vs Linlin (`--deck0`, `--deck1` change them). Replays go to `replays/`.

## Real cards

`data/cards.json` holds every English card (2,850) and `data/decks.json` the official
recommended decks (107 valid), both scraped from en.onepiece-cardgame.com.
To refresh them after a new set comes out:

```bash
python3 tools/fetch_cards.py
python3 -m optcg.play --list-decks                       # decks, by share of effect text implemented
python3 -m optcg.play --deck0 "(Red) Roronoa Zoro" --deck1 "Boa Hancock" --p1 greedy --games 200
python3 -m optcg.play --missing "(Red) Roronoa Zoro"     # what that deck's cards can't do yet
python3 -m optcg.play --deck0 mydeck.txt --games 1 --log # decklist file: one '4xOP01-016' per line
```

Every card gets its real stats, colors, types and printed keywords. Effects come from two places:

- `optcg/effect_text.py` compiles common wordings ("K.O. up to 1 of your opponent's Characters
  with a cost of 3 or less", "Look at 5 cards ...", "Choose one:", conditions, costs). It covers
  about 54% of all effect lines (all of OP-17), and 1,313 cards are fully implemented.
- `optcg/scripts.py` holds hand-written effects for cards the compiler can't read.

A line the engine can't do is skipped and listed in `CardDef.unimplemented`. Such a card still
plays with its stats and keywords. An Event whose [Main]/[Counter] text isn't implemented can't be
used at all.

## Layout

| File | What it holds |
|---|---|
| `optcg/cards.py` | `CardDef` (printed card), `Effect`, `Card` (a card in a game), keywords |
| `optcg/deck.py` | Deck construction rules (50 cards, Leader colors, max 4 copies) |
| `optcg/engine.py` | `Game`: setup, turn phases, battle, damage, effects, defeat checks |
| `optcg/actions.py` | `Decision` (a question to a player) and `Action` (an answer) |
| `optcg/agents.py` | `RandomAgent`, `GreedyAgent` (a baseline, not a strong player) |
| `optcg/cardpool.py` | Made-up sample cards and two 50-card decks |
| `optcg/carddb.py` | Loads real cards and decks, decklist parsing, coverage report |
| `optcg/effect_text.py` | Compiles printed effect text into engine effects |
| `optcg/scripts.py` | Hand-written effects for individual real cards |
| `tools/fetch_cards.py` | Downloads card data and official decklists |
| `optcg/play.py` | Runs bot-vs-bot games and terminal play |
| `optcg/human.py` | Terminal play against a bot |
| `optcg/env.py` | Training environment and observation encoding |
| `optcg/nn/` | PyTorch policy network, imitation and PPO training (needs PyTorch) |
| `optcg/replay.py` | Records a game as an HTML slideshow (`replay_viewer.html` is the page) |
| `tools/bench.py` | Games per second, with and without encoding |

## Driving the engine

```python
g = Game((RED_DECK, GREEN_DECK), seed=1)
while not g.over:
    d = g.pending                # Decision: who decides, what kind, the legal options
    g.step(bot.act(g, d))        # answer with one of d.options
print(g.winner)
```

Every decision is "pick one option", so the action space is discrete.
Choosing "up to N" cards is asked one card at a time with a Done option.
A decision with only one option is answered automatically.
`g.observe(player)` returns only what that player is allowed to see.

## Writing a card

Effects are generator functions `(game, source_card)`, so they can ask for choices midway:

```python
def ko_power_5000_or_less(g, src):
    opp = [c for c in g.characters(g.opponent(src.owner)) if g.power(c) <= 5000]
    for c in (yield from g.choose_cards(src.owner, opp, 1, "K.O. a Character")):
        yield from g.ko(c)

FLAME = CardDef("SMP-R12", "Flame Strike", Category.EVENT, ("red",), cost=3, effects=(
    Effect(Timing.MAIN, ko_power_5000_or_less, "K.O. up to 1 ..."),))
```

`Effect` carries the timing, [Once Per Turn], [DON!! xX], [Your Turn] /
[Opponent's Turn], costs (rest DON!!, DON!! −X, rest this card), extra conditions,
and whether it's optional.

## Training a bot

`optcg/env.py` wraps a game for learning: the learner plays one seat and a fixed agent the other.

```python
env = Env(find_deck("(Blue) Rocks.D.Xebec"), find_deck("(Yellow) Charlotte Linlin"), opponent=GreedyAgent())
obs = env.reset(seed=0)
while not env.done:
    obs, reward, done = env.step(policy(obs))   # an index into obs["options"]
```

An observation holds only what the learner may see, as plain lists of numbers:
`scalars` (Life, hand, DON!!, turn, ...), `cards` (one row per visible card, with a card id from
`card_index()` for an embedding) and `options` (one row per legal option, pointing at the card rows
of the acting card and its target). Score each option row and pick one; the options change at every
decision, so there is no fixed action space. Reward is +1 for a win, -1 for a loss, 0 otherwise.

### With PyTorch

The engine needs only the standard library; the neural bots in `optcg/nn/` need PyTorch, installed
in the project venv (`python3 -m venv .venv && .venv/bin/pip install torch`).

```bash
.venv/bin/python -m optcg.nn.train imitate --out runs/imitate.pt          # copy GreedyAgent first
.venv/bin/python -m optcg.nn.train ppo --init runs/imitate.pt --out runs/ppo --iters 200
.venv/bin/python -m optcg.nn.train eval runs/ppo/latest.pt --vs greedy
.venv/bin/python -m optcg.play --deck0 "(Blue) Rocks.D.Xebec" --deck1 "(Yellow) Charlotte Linlin" --human 0 --p1 runs/ppo/latest.pt
```

A run without `--league` is locked to one matchup (Rocks vs Linlin by default), so the bot learns
those two decks and nothing else. `--league` instead samples every pairing of the decks whose
effect text is fully implemented, with the learner on either side, so one bot learns them all and
far more cards see real play. Evaluations then break the win rate down per deck:

```bash
.venv/bin/python -m optcg.nn.train imitate --league --out runs/league_imitate.pt
.venv/bin/python -m optcg.nn.train ppo --league --init runs/league_imitate.pt --out runs/league --iters 200
.venv/bin/python -m optcg.nn.train eval runs/league/latest.pt --league --vs greedy
```

The network (`optcg/nn/model.py`) embeds each visible card, mixes them with one attention layer,
and scores every legal option against the pooled state; a value head predicts the result.
`ppo` plays games in parallel worker processes against GreedyAgent and frozen snapshots of
itself, on both seats, and saves `latest.pt` every iteration and `iterNNNN.pt` at each evaluation.
Checkpoints are tied to the card list in `data/cards.json`; retrain after refreshing it.

`python3 tools/bench.py` measures speed. On one core the Rocks vs Linlin matchup runs about
90 games/s bot-vs-bot and 40 games/s through the env with encoding (about 180 decisions a game).

## Known simplifications

- Replacement effects ("instead") work only for a card leaving the field or being K.O.'d (`CardDef.replacements`), and the first one the owner accepts is used.
- Infinite loops (11-1) aren't detected; `play_game` gives up after 20,000 decisions.
- Card text using the new timings (start of turn, start of Main Phase, end of battle) isn't compiled yet; only scripts can use them. Each appears on about one card.
- No fast state cloning for search bots yet. Replaying `g.history` with the same seed rebuilds a game.
- `GreedyAgent` is a yardstick, not a good player: it spends every spare DON!! on its Leader, never
  blocks or counters subtly, and its attacks are shallow. Lopsided deck matchups under greedy play
  usually say more about the heuristic than about the cards.

## Effect order

Auto effects queue when they trigger and resolve at the next safe point (8-6): effects that
trigger together resolve turn player first, each player choosing the order of their own
(an `order` decision). Effects that trigger while others resolve wait for the current batch
(8-6-1-1, 8-6-3), and effects triggered during damage wait until it is over (8-6-2).
