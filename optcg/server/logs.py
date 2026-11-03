"""Logs as one JSON object per line on stdout, for CloudWatch (or any log service) to collect and search.

    event("match_over", match="3f2a...", bot="alice-bot", reason="illegal_move")
    -> {"time": "2026-11-02T19:30:00", "level": "info", "event": "match_over", "match": "3f2a...", ...}
"""
from __future__ import annotations

import json
import logging
import sys

log = logging.getLogger("optcg.server")


class JsonFormatter(logging.Formatter):
    def format(self, r: logging.LogRecord) -> str:
        out = {"time": self.formatTime(r, "%Y-%m-%dT%H:%M:%S"), "level": r.levelname.lower(),
               "event": r.getMessage(), "logger": r.name, **getattr(r, "fields", {})}
        if r.exc_info:
            out["error"] = self.formatException(r.exc_info)
        return json.dumps(out, default=str)


def setup(level: int = logging.INFO) -> None:
    """Send every log (ours and uvicorn's) to stdout as JSON."""
    h = logging.StreamHandler(sys.stdout)
    h.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level)


def event(name: str, level: int = logging.INFO, **fields) -> None:
    log.log(level, name, extra={"fields": fields})
