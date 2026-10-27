"""Download the official English card list and recommended decklists.

    python3 tools/fetch_cards.py            # writes data/cards.json and data/decks.json

Source: https://en.onepiece-cardgame.com/cardlist/ (one POST per set) and
https://en.onepiece-cardgame.com/feature/deck/ (recommended decks with counts).
Parallel-art printings (ids ending in _p1, _p2, ...) are skipped.
"""
from __future__ import annotations

import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

BASE = "https://en.onepiece-cardgame.com"
UA = {"User-Agent": "Mozilla/5.0 (optcg-bot card importer)"}
DATA = Path(__file__).resolve().parent.parent / "data"
DELAY = 0.5  # seconds between requests


def fetch(path: str, form: dict | None = None) -> str:
    data = urllib.parse.urlencode(form).encode() if form else None
    req = urllib.request.Request(BASE + path, data=data, headers=UA)
    with urllib.request.urlopen(req, timeout=30) as r:
        body = r.read().decode("utf-8")
    time.sleep(DELAY)
    return body


def clean(fragment: str) -> str:
    fragment = re.sub(r"<br\s*/?>", "\n", fragment)
    fragment = re.sub(r"<h3>.*?</h3>", "", fragment, flags=re.S)
    text = html.unescape(re.sub(r"<[^>]+>", "", fragment))
    return "\n".join(line.strip() for line in text.splitlines() if line.strip())


def field(block: str, cls: str) -> str:
    m = re.search(rf'<div class="{cls}">(.*?)</div>', block, re.S)
    return clean(m.group(1)) if m else ""


def number(s: str):
    s = s.strip()
    return int(s) if s.lstrip("-").isdigit() else None


def parse_cards(page: str) -> list[dict]:
    cards = []
    for m in re.finditer(r'<dl class="modalCol" id="([^"]+)">(.*?)</dl>', page, re.S):
        cid, block = m.groups()
        if "_p" in cid or "_r" in cid:
            continue
        info = re.findall(r"<span>(.*?)</span>", re.search(r'<div class="infoCol">(.*?)</div>', block, re.S).group(1))
        category = html.unescape(info[2]).strip().title()
        cost_block = re.search(r'<div class="cost"><h3>(.*?)</h3>(.*?)</div>', block, re.S)
        cost_label, cost_val = (cost_block.group(1), clean(cost_block.group(2))) if cost_block else ("", "")
        attrs = re.findall(r"<i>(.*?)</i>", re.search(r'<div class="attribute">(.*?)</div>', block, re.S).group(1)) \
            if '<div class="attribute">' in block else []
        effect = field(block, "text")
        trigger = field(block, "trigger")
        cards.append({
            "id": cid,
            "name": clean(re.search(r'<div class="cardName">(.*?)</div>', block, re.S).group(1)),
            "rarity": info[1].strip(),
            "category": category,
            "colors": [c.strip().lower() for c in field(block, "color").split("/") if c.strip()],
            "cost": number(cost_val) if cost_label == "Cost" else None,
            "life": number(cost_val) if cost_label == "Life" else None,
            "power": number(field(block, "power")),
            "counter": number(field(block, "counter")),
            "attributes": [html.unescape(a).strip() for a in attrs if a.strip()],
            "types": [t.strip() for t in field(block, "feature").split("/") if t.strip()],
            "effect": "" if effect == "-" else effect,
            "trigger": trigger,
            "block": field(block, "block"),
            "set": field(block, "getInfo"),
        })
    return cards


def series_ids(page: str) -> list[tuple[str, str]]:
    sel = re.search(r'<select[^>]*name="series".*?</select>', page, re.S).group(0)
    return [(v, clean(n)) for v, n in re.findall(r'<option value="(\d+)"[^>]*>(.*?)(?=<option|</select>)', sel, re.S)]


def parse_deck(page: str) -> dict:
    title = clean(re.search(r"<title>(.*?)</title>", page, re.S).group(1)).split(" − ")[0].split(" - FEATURE")[0]
    col = re.search(r'<div class="deckCardlistCol">(.*?)</section>', page, re.S).group(1)
    cards = re.findall(r'card/([A-Z0-9]+-\d{3})[^"/]*\.png">.*?<p class="cards_\d+">x(\d+)</p>', col, re.S)
    leader, rest = cards[0][0], cards[1:]
    return {"name": title, "leader": leader, "cards": {cid: int(n) for cid, n in rest}}


def main() -> None:
    DATA.mkdir(exist_ok=True)
    first = fetch("/cardlist/")
    all_cards: dict[str, dict] = {}
    for sid, name in series_ids(first):
        page = fetch("/cardlist/", {"series": sid, "freewords": ""})
        new = [c for c in parse_cards(page) if c["id"] not in all_cards]
        for c in new:
            all_cards[c["id"]] = c
        print(f"{name[:60]:60} {len(new):4} new cards", file=sys.stderr)
    cards = sorted(all_cards.values(), key=lambda c: c["id"])
    (DATA / "cards.json").write_text(json.dumps(cards, indent=1, ensure_ascii=False))
    print(f"wrote {len(cards)} cards", file=sys.stderr)

    index = fetch("/feature/deck/")
    pages = sorted(set(re.findall(r'href="(deck_\d+\.php)"', index)))
    decks = []
    for p in pages:
        try:
            deck = parse_deck(fetch("/feature/deck/" + p))
        except (AttributeError, IndexError) as e:
            print(f"skipping {p}: {e}", file=sys.stderr)
            continue
        deck["source"] = f"{BASE}/feature/deck/{p}"
        decks.append(deck)
    (DATA / "decks.json").write_text(json.dumps(decks, indent=1, ensure_ascii=False))
    print(f"wrote {len(decks)} decks", file=sys.stderr)


if __name__ == "__main__":
    main()
