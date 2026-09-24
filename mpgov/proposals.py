"""Proposed Lexicon changes: diffing, review rendering, and export to disk.

mpgov is read-only against Mixpanel by design - it never mutates Lexicon. The
agent drafts metadata, the diff is shown for review, and the result is written
to a local file you apply yourself (Lexicon UI, or the Mixpanel API on your own
terms). Nothing here calls a Mixpanel write endpoint.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .session import GovSession

STATE_DIR = Path.home() / ".mpgov"
PROPOSAL_LOG = STATE_DIR / "proposals.jsonl"

EVENT_FIELDS = ("description", "display_name", "tags", "verified", "hidden")
PROPERTY_FIELDS = ("description", "display_name", "example_value", "sensitive", "hidden")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# --------------------------------------------------------------------- diffing


@dataclass
class EntityDiff:
    label: str
    changes: list[tuple[str, Any, Any]]

    @property
    def empty(self) -> bool:
        return not self.changes


def _diff_entity(
    defn: Any, requested: dict[str, Any], fields: tuple[str, ...]
) -> list[tuple[str, Any, Any]]:
    out: list[tuple[str, Any, Any]] = []
    for f in fields:
        if f not in requested or requested[f] is None:
            continue
        before = getattr(defn, f, None) if defn is not None else None
        after = requested[f]
        if before != after:
            out.append((f, before, after))
    return out


def diff_for(session: GovSession, args: dict[str, Any]) -> list[EntityDiff]:
    """What the proposed metadata would change, against current Lexicon."""
    diffs: list[EntityDiff] = []

    for entry in args.get("events") or []:
        name = entry.get("event")
        if not name:
            continue
        changes = _diff_entity(session.cached_event_def(name), entry, EVENT_FIELDS)
        if changes:
            diffs.append(EntityDiff(label=f"event: {name}", changes=changes))

    for entry in args.get("properties") or []:
        name = entry.get("property")
        if not name:
            continue
        rt = entry.get("resource_type", "Event")
        defn = session.cached_property_def(name, resource_type=rt)
        changes = _diff_entity(defn, entry, PROPERTY_FIELDS)
        if changes:
            diffs.append(EntityDiff(label=f"{rt.lower()} property: {name}", changes=changes))

    return diffs


# ---------------------------------------------------------------------- export


def _markdown(project: str, diffs: list[EntityDiff], note: str | None) -> str:
    lines = [
        f"# Lexicon proposals - {project}",
        "",
        f"Drafted {_now()} by mpgov. **Not applied** - review and apply in Lexicon.",
        "",
    ]
    if note:
        lines += [f"> {note}", ""]
    for d in diffs:
        lines.append(f"## {d.label}")
        lines.append("")
        for field, before, after in d.changes:
            lines.append(f"**{field}**")
            lines.append("")
            lines.append(f"- current: {_show(before)}")
            lines.append(f"- proposed: {_show(after)}")
            lines.append("")
    return "\n".join(lines)


def _show(value: Any) -> str:
    if value is None:
        return "_(empty)_"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "_(empty)_"
    s = str(value).strip()
    return s or "_(empty)_"


def save(
    session: GovSession,
    args: dict[str, Any],
    diffs: list[EntityDiff],
    *,
    note: str | None = None,
    out_dir: Path | None = None,
) -> dict[str, str]:
    """Write the proposals as Markdown (to read) and JSON (to script against)."""
    out_dir = out_dir or Path.cwd()
    stamp = time.strftime("%Y%m%d-%H%M%S")
    base = out_dir / f"lexicon-proposals-{stamp}"

    payload = {
        "generated": _now(),
        "project": session.project_label,
        "applied": False,
        "note": note,
        "proposals": args,
        "diffs": [
            {
                "entity": d.label,
                "changes": [
                    {"field": f, "current": b, "proposed": a} for f, b, a in d.changes
                ],
            }
            for d in diffs
        ],
    }

    md_path = base.with_suffix(".md")
    json_path = base.with_suffix(".json")
    md_path.write_text(_markdown(session.project_label, diffs, note))
    json_path.write_text(json.dumps(payload, indent=2, default=str))

    _log(session.project_label, payload)
    return {"markdown": str(md_path), "json": str(json_path)}


def _log(project: str, payload: dict[str, Any]) -> None:
    try:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        with PROPOSAL_LOG.open("a") as fh:
            fh.write(
                json.dumps(
                    {
                        "ts": payload["generated"],
                        "project": project,
                        "entities": [d["entity"] for d in payload["diffs"]],
                    },
                    default=str,
                )
                + "\n"
            )
    except OSError:
        pass


def read_log(limit: int = 30, project: str | None = None) -> list[dict[str, Any]]:
    if not PROPOSAL_LOG.exists():
        return []
    rows = []
    for raw in PROPOSAL_LOG.read_text().splitlines():
        try:
            row = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if project and row.get("project") != project:
            continue
        rows.append(row)
    return rows[-limit:]


def export_lexicon(session: GovSession, out_dir: Path | None = None) -> str:
    """Full Lexicon export - a read, kept as a backup/diff baseline."""
    out_dir = out_dir or Path.cwd()
    data = session.ws.export_lexicon()
    path = out_dir / f"lexicon-export-{time.strftime('%Y%m%d-%H%M%S')}.json"
    path.write_text(json.dumps(data, indent=2, default=str))
    return str(path)
