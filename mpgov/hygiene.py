"""Lexicon gap analysis. Pure logic over GovSession reads - no LLM involved.

Runnable on its own (`python -m mpgov.hygiene`) so the Mixpanel reads and the
volume join can be verified before an agent is anywhere near them.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from .policy import Policy
from .session import GovSession


@dataclass
class GapRow:
    event: str
    volume: int | None
    missing: list[str]
    description: str | None
    display_name: str | None
    tags: list[str]
    verified: bool
    hidden: bool

    @property
    def documented(self) -> bool:
        return not self.missing

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["documented"] = self.documented
        return d


def _missing_fields(defn, required: list[str]) -> list[str]:
    out = []
    for f in required:
        value = getattr(defn, f, None) if defn is not None else None
        if value is None or (isinstance(value, str) and not value.strip()):
            out.append(f)
    return out


def scan(
    session: GovSession,
    policy: Policy,
    *,
    include_hidden: bool = False,
) -> list[GapRow]:
    """Every non-ignored event, with its documentation gaps and 30d volume."""
    names = [n for n in session.event_names() if not policy.is_ignored(n)]
    defs = session.event_defs(names)
    frm, to = policy.volume_window
    volumes = session.volumes(days=int(policy["volume_days"]), frm=frm, to=to)
    required = list(policy["required_event_fields"])

    rows: list[GapRow] = []
    for name in names:
        defn = defs.get(name)
        hidden = bool(getattr(defn, "hidden", False))
        if hidden and not include_hidden:
            continue
        rows.append(
            GapRow(
                event=name,
                volume=(None if volumes.get(name) is None else int(volumes[name])),
                missing=_missing_fields(defn, required),
                description=getattr(defn, "description", None),
                display_name=getattr(defn, "display_name", None),
                tags=list(getattr(defn, "tags", None) or []),
                verified=bool(getattr(defn, "verified", False)),
                hidden=hidden,
            )
        )

    # Most gaps first, then highest volume. Unknown volume sorts last within a
    # gap tier rather than masquerading as zero.
    rows.sort(key=lambda r: (-len(r.missing), r.volume is None, -(r.volume or 0)))
    return rows


def summarize(rows: list[GapRow]) -> dict[str, Any]:
    total = len(rows)
    documented = sum(1 for r in rows if r.documented)
    by_field: dict[str, int] = {}
    for r in rows:
        for f in r.missing:
            by_field[f] = by_field.get(f, 0) + 1
    measured = [r for r in rows if r.volume is not None]
    undocumented_volume = sum(r.volume for r in measured if r.missing)
    total_volume = sum(r.volume for r in measured) or 1
    return {
        "events_scanned": total,
        "fully_documented": documented,
        "with_gaps": total - documented,
        "coverage_pct": round(100.0 * documented / total, 1) if total else 100.0,
        "missing_by_field": by_field,
        "pct_of_volume_undocumented": (
            round(100.0 * undocumented_volume / total_volume, 1) if measured else None
        ),
        "events_with_unmeasured_volume": total - len(measured),
        "untagged": sum(1 for r in rows if not r.tags),
        "unverified": sum(1 for r in rows if not r.verified),
    }


def _main() -> int:
    import argparse
    import sys

    from .render import console, gap_table, summary_panel

    ap = argparse.ArgumentParser(prog="python -m mpgov.hygiene")
    ap.add_argument("--limit", type=int, default=20, help="rows to print")
    ap.add_argument("--project", help="project id")
    ap.add_argument("--policy", help="path to policy.yaml")
    ap.add_argument("--include-hidden", action="store_true")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    ap.add_argument("--refresh", action="store_true", help="ignore cached reads")
    args = ap.parse_args()

    from . import cache
    from .session import NotAuthenticated, RateLimited

    try:
        sess = GovSession.connect(project=args.project)
    except NotAuthenticated as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.refresh:
        cache.clear(sess.project_id)

    policy = Policy.load(args.policy)
    try:
        rows = scan(sess, policy, include_hidden=args.include_hidden)
    except RateLimited as exc:
        print(exc, file=sys.stderr)
        return 2
    stats = summarize(rows)

    if args.json:
        import json

        print(json.dumps({"summary": stats, "rows": [r.to_dict() for r in rows[: args.limit]]}, indent=2))
    else:
        console.print(summary_panel(sess.project_label, stats, policy.source, sess.volume_source))
        console.print(gap_table(rows[: args.limit]))
    sess.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
