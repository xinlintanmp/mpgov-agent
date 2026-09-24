"""On-disk cache for Lexicon reads.

Mixpanel's discovery endpoints are rate limited hard - once exhausted, the retry
window is an hour. A full scan costs an `events()` call plus a
`get_event_definitions` call per 100 events plus a volume call, so re-fetching
all of that on every `mpgov` launch is what burns the quota. Cached reads make
relaunching free and keep an interrupted cleanup session resumable.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

ROOT = Path.home() / ".mpgov" / "cache"
DEFAULT_TTL_HOURS = 6


def _path(project: str, key: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in str(project))
    return ROOT / safe / f"{key}.json"


def get(project: str, key: str, *, ttl_hours: float = DEFAULT_TTL_HOURS) -> Any | None:
    """Cached value, or None if absent, unreadable or stale."""
    p = _path(project, key)
    if not p.is_file():
        return None
    try:
        blob = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    if not isinstance(blob, dict) or "ts" not in blob:
        return None
    if ttl_hours >= 0 and (time.time() - blob["ts"]) > ttl_hours * 3600:
        return None
    return blob.get("value")


def put(project: str, key: str, value: Any) -> None:
    p = _path(project, key)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(json.dumps({"ts": time.time(), "value": value}, default=str))
        tmp.replace(p)
    except OSError:
        pass  # a cache that cannot be written is not an error


def age_hours(project: str, key: str) -> float | None:
    p = _path(project, key)
    if not p.is_file():
        return None
    try:
        blob = json.loads(p.read_text())
        return (time.time() - blob["ts"]) / 3600
    except (json.JSONDecodeError, OSError, KeyError):
        return None


def clear(project: str | None = None) -> int:
    """Drop cached reads. Returns the number of files removed."""
    target = ROOT if project is None else _path(project, "x").parent
    if not target.exists():
        return 0
    n = 0
    for f in target.rglob("*.json"):
        try:
            f.unlink()
            n += 1
        except OSError:
            pass
    return n
