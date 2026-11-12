"""Render a matchup table that `optcg.nn.train` wrote (ppo --decks: runs/<out>/table_latest.json;
eval --decks --table FILE): rows are the bot's deck, columns the opponent's (GreedyAgent).

    python3 tools/matchup_table.py runs/league15/table_latest.json             # win rates
    python3 tools/matchup_table.py runs/league15/table_latest.json --edge      # minus greedy's own
    python3 tools/matchup_table.py runs/league15/table_latest.json --html table.html --open

The edge is the bot's win rate minus greedy's win rate with the same decks (greedy vs greedy), so
it says how much better than greedy the bot plays a matchup, whatever the decks' strengths.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from optcg.nn.league import format_table, short, summary  # noqa: E402


def _color(x, edge: bool) -> str:
    """Red for bad, white for even, green for good."""
    if x is None:
        return "#eee"
    v = max(-1.0, min(1.0, 2 * x if edge else 2 * x - 1))  # edge +-50% or win rate 0..100% -> -1..1
    r, g = (255, int(255 * (1 + v))) if v < 0 else (int(255 * (1 - v)), 255)
    b = int(255 * (1 - abs(v)))
    return f"rgb({r},{g},{b})"


def to_html(t: dict, key: str) -> str:
    edge = key == "edge"
    names = [short(d) for d in t["decks"]]
    head = "".join(f"<th title='{html.escape(n)}'>{i + 1}</th>" for i, n in enumerate(names))
    rows = []
    for i, (n, row) in enumerate(zip(names, t[key])):
        cells = "".join(f"<td style='background:{_color(x, edge)}'>"
                        f"{'' if x is None else f'{100 * x:+.0f}' if edge else f'{100 * x:.0f}'}</td>" for x in row)
        rows.append(f"<tr><th class=n>{i + 1}. {html.escape(n)}</th>{cells}</tr>")
    title = "Edge over greedy" if edge else "Win rate vs greedy"
    return f"""<!doctype html><meta charset=utf-8><title>Matchup table</title>
<style>body{{font:14px system-ui;margin:24px;background:#fff;color:#111}}table{{border-collapse:collapse}}
td,th{{padding:4px 6px;text-align:center;border:1px solid #ccc;min-width:28px}}th.n{{text-align:left}}</style>
<h1>{title} (%)</h1><p>{html.escape(summary(t))}. Rows: the bot's deck; columns: greedy's deck.</p>
<table><tr><th></th>{head}</tr>{''.join(rows)}</table>"""


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("table", help="a table JSON from optcg.nn.train")
    ap.add_argument("--edge", action="store_true", help="show the edge over greedy instead of win rates")
    ap.add_argument("--html", help="also write a colored HTML table here")
    ap.add_argument("--open", action="store_true", help="open the HTML table in a browser")
    args = ap.parse_args(argv)
    t = json.loads(Path(args.table).read_text())
    key = "edge" if args.edge else "win"
    if key not in t:
        raise SystemExit(f"{args.table} has no {key!r} table (it was saved without a greedy baseline)")
    print(summary(t))
    print(format_table(t, key))
    if args.html:
        Path(args.html).write_text(to_html(t, key))
        print(f"wrote {args.html}")
        if args.open:
            webbrowser.open(Path(args.html).resolve().as_uri())


if __name__ == "__main__":
    main()
