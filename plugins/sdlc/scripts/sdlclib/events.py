"""Audit trail: one JSON object per line in `.sdlc/logs/events.jsonl` (same shape the hooks write)."""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterator, Optional

from .util import append_line, now_iso


def log_event(root: str, event: str, decision: str, reason: str = "", detail: Optional[Dict[str, Any]] = None,
              session_id: str = "") -> None:
    line = json.dumps({"ts": now_iso(), "event": event, "session_id": session_id, "decision": decision,
                       "reason": reason, "detail": detail}, ensure_ascii=False)
    try:
        append_line(os.path.join(root, ".sdlc", "logs", "events.jsonl"), line)
    except OSError:
        pass


def read_events(root: str, event: str = "") -> Iterator[Dict[str, Any]]:
    path = os.path.join(root, ".sdlc", "logs", "events.jsonl")
    try:
        fh = open(path, encoding="utf-8", errors="replace")
    except OSError:
        return
    with fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and (not event or ev.get("event") == event):
                yield ev
