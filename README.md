# optcg-bot

A ONE PIECE CARD GAME rules engine and bots, written from the official
Comprehensive Rules v1.2.1 (`rules/`). Pure Python 3.10, no dependencies.

```bash
python3 -m optcg.play --games 200 --p0 greedy --p1 random   # bot vs bot win rates
python3 -m optcg.play --games 1 --log                        # print one game's log
python3 -m unittest discover tests                           # rule tests
```

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
| `optcg/agents.py` | `RandomAgent`, `GreedyAgent` |
| `optcg/cardpool.py` | Made-up sample cards and two 50-card decks |
| `optcg/carddb.py` | Loads real cards and decks, decklist parsing, coverage report |
| `optcg/effect_text.py` | Compiles printed effect text into engine effects |
| `optcg/scripts.py` | Hand-written effects for individual real cards |
| `tools/fetch_cards.py` | Downloads card data and official decklists |
| `optcg/play.py` | Runs bot-vs-bot games |

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

## Known simplifications

- When several auto effects trigger together, they resolve turn player first (8-6-1), but each player's own effects resolve in field order instead of the order they choose.
- Counter cards can only boost the card under attack, not another Leader or Character.
- DON!! −X chooses which DON!! to return automatically: rested first, then active, then given.
- Replacement effects ("instead") work only for a card leaving the field or being K.O.'d (`CardDef.replacements`), and the first one the owner accepts is used.
- Not modelled yet: start-of-turn and start-of-Main-Phase triggers, end-of-battle triggers, "cannot be K.O.'d" style protections, and face-up Life cards.
- No fast state cloning for search bots yet. Replaying `g.history` with the same seed rebuilds a game.
