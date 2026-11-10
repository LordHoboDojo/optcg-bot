# Tools

Scripts in `tools/` and the Rust examples in `engine-rs/examples/`. Run the Python ones from the
project folder with the project's virtualenv (`.venv/bin/python tools/...`).

| Tool | What it does | Writes |
|---|---|---|
| `tools/fetch_cards.py` | Downloads the official English card list (one request per set) and the recommended decklists; skips parallel-art printings | `data/cards.json`, `data/decks.json` |
| `tools/analysis.py` | Every pair of bots plays a set of games (seats alternate, Rocks vs Linlin); Elo-style ratings are fitted to the results (Bradley-Terry, anchored on greedy at 1500), and each training run's progress is collected | `data/analysis.json`, read by the site's Analysis page and Play page |
| `tools/bench.py` | How fast the engine plays a game, which sets how long training takes | |
| `tools/bot_client.py` | A working example bot for the [bot API](../bot-api.md): connects with `OPTCG_KEY`, sends `start`, answers each `decision` with a random option, prints `error` and `over` | |
| `tools/lethal.py` | How often a bot takes lethal when it has it ([PR #4](https://github.com/LordHoboDojo/optcg-bot/pull/4), below) | optional JSON summary |
| `cargo run --release --example coverage` | Card text the Rust engine doesn't implement (`-- --list` for each line) | |
| `cargo run --release --example dump -- PREFIX` | How the second compiler read each line, for cards whose id starts with `PREFIX` | |
| `cargo run --release --example try -- "sentence"` | Which parts of a sentence the second compiler reads | |
| `cargo run --release --example fuzz` | Random games with random decks of implemented cards, looking for panics and games that never end | |
| `cargo run --release --example bench` | Speed of random games and of copying a game in progress | |

## Card data

```mermaid
flowchart LR
    site["Official card list<br/>and recommended decks"] -->|tools/fetch_cards.py| cards["data/cards.json"]
    site -->|tools/fetch_cards.py| decks["data/decks.json"]
    cards --> rs["optcg_rs.Db<br/>(compile every card)"]
    cards --> py["optcg.carddb.load_cards"]
    decks --> official["carddb.official_decks()"]
    cards --> pool["GET /api/cards<br/>(deck builder)"]
```

After refreshing `data/cards.json`, run `cargo run --release --example coverage` to see whether
any new card text isn't implemented yet.

## Bot analysis

```mermaid
flowchart LR
    ckpt["checkpoints (runs/, models/)"] --> an["tools/analysis.py --games N"]
    an -->|"every pair, both seats,<br/>on the Rust engine"| json["data/analysis.json<br/>bots, ratings, pairs, runs"]
    json --> api["GET /api/analysis, GET /api/options"]
    api --> pages["Analysis page, Play page<br/>(house bots ordered by rating)"]
```

The server matches a house bot to its rating by the SHA-256 of its checkpoint file, so a
renamed file keeps its rating. Refresh and commit `data/analysis.json` after training new
checkpoints.

## Lethal checker (PR #4)

> `tools/lethal.py` and `engine-rs/src/lethal.rs` are in
> [PR #4](https://github.com/LordHoboDojo/optcg-bot/pull/4) (branch `lethal-checker`), not yet on
> `main`. The search itself is described in [Rust engine](engine.md#lethal-search-lethalrs).

It measures how often a bot finds a win that is there to be taken: of the turns where the bot
had a forced win, how many did it win?

```bash
.venv/bin/python tools/lethal.py runs/selfplay/best.pt models/house.pt greedy --games 200
.venv/bin/python tools/lethal.py runs/selfplay/best.pt --show 5    # print missed turns
```

```mermaid
sequenceDiagram
    participant T as tools/lethal.py
    participant W as Worker process
    participant G as optcg_rs.Game
    participant L as lethal.rs forced_win
    T->>W: one job per game (bot, opponent, seed, seat, decks)
    loop each decision of the game
        alt the bot's first Main decision of its turn,<br/>opponent on at most --max-life Life
            W->>L: forced_win(seat, --budget)
            L-->>W: true, false, or None (budget spent), positions searched
            W->>W: note the turn, then see whether the bot wins during it
        end
        W->>G: step(the agent's pick)
    end
    W-->>T: the checked turns
    T->>T: per bot: took lethal X of Y forced turns,<br/>wins on unsettled turns, wins with no forced win
```

Options: `--vs` (the opponent, default `greedy`), `--games` (per bot; seats and deck pairings
alternate), `--max-life` (default 2), `--budget` (positions per search, default 20,000),
`--workers`, `--seed`, `--show N` (print N missed turns with their log), `--json` (write the
summary).

"Forced" is against the cards actually there: the search sees the opponent's hand and the deck
order, so a bot that holds back for fear of a counter the opponent doesn't have counts as missing
lethal.
