# optcg-rs: the rules engine in Rust

A port of the Python engine in `optcg/` (engine, card-text compiler and hand-written scripts),
built as the Python module `optcg_rs`. It plays exactly the same games as the Python engine:
same seed and same choices give the same state, options, prompts and log, decision by decision.
That's checked by `tests/test_rust_engine.py` and `tests/rust_parity.py`.

## Building

Needs a Rust toolchain (https://rustup.rs) and `maturin` in the project's virtualenv.

```bash
cd engine-rs
env -u CONDA_PREFIX VIRTUAL_ENV=../.venv ../.venv/bin/maturin develop --release
cargo test --release                      # Rust tests (no Python needed)
cargo run --release --example bench       # speed
```

## Using it from Python

```python
import optcg_rs
db = optcg_rs.Db("data/cards.json")                 # compile every card once
g = optcg_rs.Game(db, [(leader_id, card_ids), (leader_id, card_ids)], seed)
g.pending_json()      # the decision: player, kind, prompt, options
g.step(2)             # answer with option number 2
g.snapshot_json()     # the whole state as JSON
g.fork()              # fast copy for search: no log, no labels
g.determinize(p, s)   # reshuffle what player p can't see; False if that shuffle doesn't fit
```

Decks are passed as card ids in the order the Python `Deck` holds them, so both engines create
and shuffle the same cards.

## How it differs inside

The Python engine pauses a generator at each decision. Paused generators can't be copied, so
the Rust engine runs in segments instead: each segment starts from a saved state at a Main Phase
decision, and `choose()` either returns the next recorded answer or stops with the decision.
Answering re-runs the segment from its checkpoint. Segments are one Main Phase action long, so
this is cheap, and the whole game is plain data that copies in a couple of microseconds.

Effects are data rather than closures: the compiler builds a tree of templates, conditions,
filters and costs (`effects.rs`) that the engine interprets. Scripts are plain functions.

`random.Random` is reimplemented bit for bit (`rng.rs`), including `shuffle`, `randrange` and
`sample`, which is what makes the two engines comparable.

## Speed (M2 Max, one core, Rocks vs Linlin, random play)

| | Python | Rust |
|---|---|---|
| One decision | ~43 us | 4.1 us (logs and labels off), 8.3 us (on) |
| Copy a game in progress | ~2,600 us (replay) | 1.8 us (`fork`) |
| Copy + reshuffle hidden cards | n/a | 15 us |

Most of the remaining time is copying the state at segment boundaries and recomputing statics.

## Known differences from Python

None in behaviour. One Python bug is kept on purpose so the engines stay comparable: the
"Return up to N Characters with a [Trigger] to the owner's hand" template reads its Trigger
group as empty, so the restriction never applies (`compile.rs`, `bounce`). Fix it in both.
