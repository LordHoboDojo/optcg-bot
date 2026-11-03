# Bot API (protocol 1)

Your program plays a match against one of the server's house bots over a WebSocket. The server
runs the game and referees it; your bot only answers decisions. It sees what its seat may see:
its own hand, both boards, face-up Life cards and trash, and of the house bot's decisions only
the option it picked.

`tools/bot_client.py` is a working client to start from.

## Getting a key

Sign in on the site, open **Your bots**, register a bot and create an API key. The key is shown
once. Keep it secret; revoke it on the same page if it leaks.

## Connecting

```
wss://<server>/api/bot/v1/play
Authorization: Bearer optcg_xxxxxxxx_...
```

One connection plays one match. A bot may play 2 matches at once (the server's
`OPTCG_BOT_MATCHES`). All messages are JSON text, up to 64 KB.

## Messages

Within 30 seconds of connecting, send:

```json
{"type": "start", "deck": "rocks", "opponent": "greedy", "opponent_deck": "(Yellow) Charlotte Linlin", "seat": 0}
```

- `deck`: a deck name from `GET /decks`, or a decklist, one card per line (`4xOP01-016` or
  `4 OP01-016 Name`, Leader included).
- `opponent` (optional): one of the house bots in `GET /decks`. The first is the default.
- `opponent_deck` (optional): a name or a decklist, the same as `deck`. The default is the
  server's house deck.
- `seat` (optional): 0 or 1. Random if you leave it out. Who goes first is decided in the game:
  one player is chosen to pick first or second (a decision of kind `go_first`).

The server answers with the match:

```json
{"type": "match", "protocol": 1, "match": "3f2a9c0d1e4b5a67", "seat": 0, "opponent": "greedy",
 "players": [{"leader": "Rocks.D.Xebec", "id": "OP15-001", "agent": "you"}, ...],
 "cards": {"OP15-001": {...}, ...}, "timeout": 30}
```

`cards` describes every card in both decks, by card id: `n` name, `cat` category, `col` colours,
`cost`, `pow` power, `cnt` counter, `life`, `ty` types, `txt` effect text, `trg` trigger text, and
`un` the parts of its text the engine doesn't implement yet.

Then, every time it's your turn to decide:

```json
{"type": "decision", "decision": 12, "kind": "main", "prompt": "Your Main Phase",
 "options": ["Play Zoro (OP01-025)", "Attack with your Leader", "End turn"],
 "actions": [["play", 31, null, null, "OP01-025", null], ...],
 "frames": [...]}
```

- `decision`: the number to quote in your answer.
- `options`: what you may do, in words. Answer with an index into this list.
- `actions`: the same options as `[kind, card, target, index, card id, target id]`. `card` and
  `target` are card uids (unique per card in the match).
- `frames`: everything that happened since your last decision, one frame per decision either
  player made, oldest first. The last one is the board as it is now.

A frame:

```json
{"t": 3, "ph": "main", "tp": 0, "log": ["P0 plays Zoro"], "b": [31, 4],
 "s": [<seat 0's side>, <seat 1's side>],
 "d": {"pl": 1, "kind": "block", "q": "...", "o": ["No block"], "n": 2, "pick": 0, "hl": [4]}}
```

`t` is the turn, `ph` the phase, `tp` whose turn it is, `log` the game log lines since the last
frame, `b` the attacker and target uids during a battle. `d` is the decision asked in that frame:
for the house bot, `o` holds only the option it chose (none for secret orderings) and `n` how
many it had.

A side:

```json
{"L": {"u": 0, "i": "OP15-001", "p": 5000, "n": 1}, "C": [{"u": 31, "i": "OP01-025", "p": 5000, "c": 3, "r": 1}],
 "S": null, "H": [{"u": 12, "i": "OP01-016", "c": 2, "p": 3000, "x": 1000}, {}], "life": [null, null, "OP01-013"],
 "trash": ["OP01-020"], "deck": 38, "don": [3, 2, 5]}
```

Cards: `u` uid, `i` card id, `p` power, `c` cost, `r` rested, `n` DON!! attached, `k` keywords,
`x` counter value, `rv` revealed. The house bot's hand cards are `{}` (face down) unless
revealed. `life` lists Life cards from the top, `null` when face down. `don` is
`[active, rested, left in the DON!! deck]`.

Answer with:

```json
{"type": "move", "decision": 12, "option": 2}
```

When the match ends:

```json
{"type": "over", "match": "3f2a9c0d1e4b5a67", "winner": 1, "result": "loss", "reason": "normal",
 "turns": 9, "frames": [...], "replay": "/matches/3f2a9c0d1e4b5a67"}
```

`reason` is `normal`, `illegal_move`, `timeout`, `disconnect` or `concede`. The server then closes
the connection. Open `replay` in a browser to watch the match from your seat.

You may send `{"type": "concede"}` at any time.

## Rules the server enforces

| You send | What happens |
|---|---|
| A move for the waiting decision whose `option` isn't one of the offered indexes (out of range, not a whole number) | `{"type": "error", "error": "illegal_move"}`, then the match is **over: you lose** |
| No move within `timeout` seconds | `error: timeout`, then you lose |
| You disconnect mid-match | You lose |
| A move for a different `decision` (a retry, or a guess at a later one) | `error: stale_decision` with the waiting `decision`; the match goes on |
| Something that isn't JSON, or not a `move` | `error: bad_request`; the match goes on |

Other messages: no or bad key at connect, `error: unauthorized` then close 4401. Too many
matches at once, `error: too_many_matches` then close 4429. A bad start message,
`error: bad_request` then close 4400.

Errors and bad requests don't stop the clock: the time limit runs from when the decision was sent.

## Versions

The server speaks the protocol number in the `match` message. A change that would break a
client gets a new number and a new URL (`/api/bot/v2/play`); new fields may appear in any
message without one, so ignore fields you don't know.
