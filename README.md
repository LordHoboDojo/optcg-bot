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

`optcg.replay` records a game and writes it as one HTML file you open in a browser. It plays
back on the same table as live games (see below), one step per decision, with both hands face
up and the option each player chose highlighted. Space plays, the arrow keys step, and the
timeline on the right jumps anywhere; click a trash pile to see everything in it. Games run on
the Rust engine (build it first, see `engine-rs/README.md`), so every card plays.

Card art comes from `StreamingAssets/Cards/<set>/<id>.png|jpg`. The page links to it by relative
path instead of embedding it, so a replay shows art only while it sits where it can reach that
folder (anywhere inside this project works). Without it the viewer draws plain text cards.

```bash
.venv/bin/python -m optcg.replay --p0 greedy --p1 greedy --seed 3 --open
.venv/bin/python -m optcg.replay --p0 runs/ppo/latest.pt --p1 greedy --seed 3 --out replays/net.html --open
```

Decks default to Rocks vs Linlin (`--deck0`, `--deck1` change them). Replays go to `replays/`.

## Playing in your browser

`optcg.web` runs a game on the Rust engine on your machine and shows it on the same board as the
replays, with you in one seat and a bot in the other:

```bash
.venv/bin/python -m optcg.web --open
.venv/bin/python -m optcg.web --deck0 "(Black) Monkey.D.Luffy" --deck1 "(Blue) Rocks.D.Xebec" --bot runs/league/latest.pt --open
```

The table follows the official playmat (zones cut from the leader's playmat in
`StreamingAssets/Playmats`), with the opponent across from you. The cards you can use glow green:

- **Play a card:** drag it from your hand onto the field, or click it.
- **Attack:** drag an attacker onto a target (valid ones pulse red), or click it and choose Attack.
- **Give DON!!:** drag an active DON!! from your cost area onto a card, or click the card.
- **Block and Counter:** Blockers glow when you're attacked; Counter cards in hand show their
  +value; the side panel says how much you need.
- **Mulligan, Trigger reveals, deck searches and ordering** open in the middle of the table.

Hover any card for a large preview and its text on the left. After each of your moves the bot's
turn plays back with its moves narrated; "Skip to my move" or End jumps ahead, and the timeline
steps back through the whole game.

You only see what your seat may see: the bot's hand is face down, and of each bot decision
only the option it picked. `--human 1` takes the other seat, `--seed` fixes the shuffle, and
the page has Concede and New game buttons.

## Hosting it

`optcg.server` is the same table as a website: each visitor plays a house bot in their own game,
signing in with Google is optional, and signed-in people can register bots that play the house
bots over a WebSocket API. See `docs/hosting.md` (Docker, Caddy, AWS) and `docs/bot-api.md`.

```bash
.venv/bin/pip install -r requirements-server.txt
OPTCG_DEV_LOGIN=1 .venv/bin/python -m optcg.server                 # http://127.0.0.1:8000
OPTCG_KEY=optcg_... .venv/bin/python tools/bot_client.py -v         # an example remote bot
```

## Real cards

`data/cards.json` holds every English card (2,850) and `data/decks.json` the official
recommended decks (107 valid), both scraped from en.onepiece-cardgame.com.
To refresh them after a new set comes out:

```bash
python3 tools/fetch_cards.py
.venv/bin/python -m optcg.play --list-decks             # decks, by share of effect text implemented
python3 -m optcg.play --deck0 "(Red) Roronoa Zoro" --deck1 "Boa Hancock" --p1 greedy --games 200
.venv/bin/python -m optcg.play --missing "(Red) Roronoa Zoro"  # what that deck's cards can't do yet
python3 -m optcg.play --deck0 mydeck.txt --games 1 --log # decklist file: one '4xOP01-016' per line
```

Every card gets its real stats, colors, types and printed keywords. Effects come from two places:

- `optcg/effect_text.py` compiles common wordings ("K.O. up to 1 of your opponent's Characters
  with a cost of 3 or less", "Look at 5 cards ...", "Choose one:", conditions, costs). It covers
  about 54% of all effect lines (all of OP-17), and 1,313 cards are fully implemented.
- `optcg/scripts.py` holds hand-written effects for cards the compiler can't read.

New card text goes into the Rust engine only. Its second compiler (`engine-rs/src/compile2.rs`)
reads the lines the first can't, as general pieces (which cards, what happens to them, for how
long, under what condition), and `engine-rs/src/scripts2.rs` holds the few one-offs, so the Rust
engine implements every card (2,850), against Python's 1,450 cards (1,557 effect lines missing).
Seven card texts in the data lack their attribute name (OP03-032, OP04-042, OP06-026, ST32-001/003/
004/005, all swordsmen); the Rust engine reads them as <Slash>. See `engine-rs/README.md`.

The tools follow the Rust engine once it is built: `--list-decks` and `--missing` report its
coverage (every official deck at 100%), `--league` trains on every official deck, and the browser
game and replays run on it. The Python engine stays as the reference the Rust one is tested
against, and still runs `optcg.play` games and `--human` in the terminal, where cards it doesn't
implement play with their stats and keywords only. The made-up sample decks (`sample-red`,
`sample-green`) exist only in the Python engine.

A line the Python engine can't do is skipped and listed in `CardDef.unimplemented` (the Rust
engine's list is `optcg_rs.Db.unimplemented(card_id)`, empty for every card). Such a card still
plays with its stats and keywords. An Event whose [Main]/[Counter] text isn't implemented can't be
used at all.

## Layout

[`docs/architecture/`](docs/architecture/README.md) explains how the parts fit together, with
diagrams: the engine and the card compiler, training and search, the site, its database and its
deployment.

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
| `optcg/search.py` | The network plus search (information-set MCTS on the Rust engine) |
| `optcg/replay.py` | Records a game as an HTML page; `replay_viewer.html` is the table, for replays and live play |
| `optcg/web.py` | Local server for playing a bot in the browser, on the replay page |
| `engine-rs/` | The same engine in Rust, as the Python module `optcg_rs` (see its README) |
| `tools/bench.py` | Games per second, with and without encoding |

## The Rust engine

`engine-rs/` is a port of the engine, compiler and scripts to Rust, built as the Python module
`optcg_rs`. Same seed and same choices give the same game in both engines, checked decision by
decision by `tests/test_rust_engine.py`. It runs a decision in about 4 us (Python: ~43 us) and
copies a game in progress in about 2 us, for search. Training uses it when it is built.
Build it with `cd engine-rs && maturin develop --release`; details in `engine-rs/README.md`.

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

Games run on the Rust engine when it is built (`engine-rs/`), else on the Python one; `--engine`
picks. Both give the same games and observations, so checkpoints work with either. Each worker
plays `--parallel` games at once (default 16) and scores their pending moves in one batched
network call: one call per move costs ~0.4 ms, batched ~0.05 ms, against ~0.01 ms for the Rust
engine itself. On one core that makes rollouts and evaluation about 3.7x faster than one Python
game at a time; with 11 workers and 128 games an iteration, the GPU update is now the larger part.

A run without `--league` is locked to one matchup (your Rocks list, `decks/rocks.txt`, vs Linlin by default), so the bot learns
those two decks and nothing else. `--league` instead samples every pairing of the official decks
whose effect text the engine fully implements (on the Rust engine, all 107: 5,671 matchups), with
the learner on either side, so one bot learns them all and far more cards see real play. Evaluations then break the win rate down per deck:

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

### Search on top of the network

`optcg/search.py` is a second bot built from the same checkpoint: at each decision it imagines
the cards it can't see (the opponent's hand, both decks, face-down Life, reshuffled to fit
everything it has seen, revealed cards included) and plays the game forward in many such worlds
on the Rust engine, one search tree shared by all of them (information-set MCTS). The network's
move scores are the priors and its value head scores where a simulation stops, as in AlphaZero.
The plain network bot is unchanged, so the two compare directly:

```bash
.venv/bin/python -m optcg.search runs/rocks/latest.pt --vs net --games 200   # search vs no search
.venv/bin/python -m optcg.search runs/rocks/latest.pt --vs greedy --sims 400
.venv/bin/python -m optcg.web --bot mcts:runs/rocks/latest.pt --open         # play against it
```

`--sims` (default 200) is simulations per decision; leaves are scored 8 to a network call.
It needs the Rust engine. In the browser the server keeps a Rust copy of the game in step.

`python3 tools/bench.py` measures speed. On one core the Rocks vs Linlin matchup runs about
90 games/s bot-vs-bot and 40 games/s through the env with encoding (about 180 decisions a game).

## Known simplifications

- Replacement effects ("instead") work for a card leaving the field or being K.O.'d (`CardDef.replacements`) and for taking damage (`CardDef.damage_replacements`); the first one the owner accepts is used.
- Readings the Comprehensive Rules don't settle, chosen and kept consistent:
  - "If you would take damage ... instead" (EB05-052 Gloriosa) is offered for each point of damage,
    since 4-6-2-2 processes X damage as X single points, including the one that would defeat you.
  - "Removed from the field by your opponent" (EB05-061 Nami) covers their effects and a K.O. in
    battle; 36 other cards say "by your opponent's effect" when they mean effects only.
  - A cost that is replaced or prevented (a K.O. stopped by "cannot be K.O.'d" or by a replacement
    effect) cancels the effect after the colon (8-3-1-7). Trashing your own Characters as a cost
    moves them directly, without offering replacement effects.
  - "A card or B card" filters are split only when nothing else qualifies them; "yellow Character
    or Stage card with a cost of 2 or less" stays unimplemented rather than guessing what each
    qualifier covers.
- Infinite loops (11-1) aren't detected; `play_game` gives up after 20,000 decisions.
- Card text using the new timings (start of turn, start of Main Phase, end of battle) isn't compiled yet; only scripts can use them. Each appears on about one card.
- No fast state cloning in the Python engine; the Rust engine (`engine-rs/`) has `fork()`.
- `GreedyAgent` is a yardstick, not a good player: it spends every spare DON!! on its Leader, never
  blocks or counters subtly, and its attacks are shallow. Lopsided deck matchups under greedy play
  usually say more about the heuristic than about the cards.

## Effect order

Auto effects queue when they trigger and resolve at the next safe point (8-6): effects that
trigger together resolve turn player first, each player choosing the order of their own
(an `order` decision). Effects that trigger while others resolve wait for the current batch
(8-6-1-1, 8-6-3), and effects triggered during damage wait until it is over (8-6-2).
