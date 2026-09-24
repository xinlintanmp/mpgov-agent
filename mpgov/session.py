"""Mixpanel workspace lifecycle, project selection, and definition caching.

Everything that talks to Mixpanel goes through here so the rest of the package
never constructs a Workspace of its own, and so reads are cached for the diffing
that `approval.py` does before a write.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Iterator

import mixpanel_headless as mh

from . import cache

# get_event_definitions/get_property_definitions take explicit name lists, so
# every full-project read has to be chunked.
CHUNK = 100

# A project's display name effectively never changes; cache it for a month so
# startup needs no /me call.
LABEL_TTL_HOURS = 24 * 30


class NotAuthenticated(RuntimeError):
    """No usable Mixpanel account is configured."""


class RateLimited(RuntimeError):
    """Mixpanel's read quota is exhausted."""

    def __init__(self, exc: Exception) -> None:
        self.retry_after = _retry_after(str(exc))
        mins = int((self.retry_after or 0) / 60)
        when = f" Try again in about {mins} minutes." if mins else ""
        super().__init__(
            f"Mixpanel rate limit reached.{when}\n"
            "Cached reads still work - mpgov will use them where it can. "
            "A full re-scan needs the quota to recover."
        )


def _retry_after(message: str) -> int | None:
    m = re.search(r"[Rr]etry after (\d+)", message)
    return int(m.group(1)) if m else None


def _chunks(items: list[str], size: int = CHUNK) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


@dataclass
class GovSession:
    """A bound Mixpanel project plus the caches the agent reads through."""

    ws: mh.Workspace
    project_label: str = ""
    project_id: str = ""
    cache_ttl_hours: float = cache.DEFAULT_TTL_HOURS
    use_cache: bool = True
    stale: bool = False
    _events: list[str] | None = field(default=None, repr=False)
    _event_defs: dict[str, mh.EventDefinition] = field(default_factory=dict, repr=False)
    _prop_defs: dict[tuple[str, str], mh.PropertyDefinition] = field(
        default_factory=dict, repr=False
    )
    _volumes: dict[str, int | None] | None = field(default=None, repr=False)
    volume_source: str = field(default="not measured", repr=False)
    _volume_key: str | None = field(default=None, repr=False)

    # ---------------------------------------------------------------- lifecycle

    @classmethod
    def connect(cls, *, account: str | None = None, project: str | None = None) -> "GovSession":
        try:
            ws = mh.Workspace(account=account, project=project)
        except (mh.ConfigError, mh.AuthenticationError) as exc:
            raise NotAuthenticated(
                f"{exc}\n\nRun `mp login` to connect a Mixpanel account, then start mpgov again."
            ) from exc
        return cls(ws=ws, project_label=_describe(ws), project_id=_project_id(ws))

    def switch_project(self, project: str) -> str:
        self.ws.use(project=project)
        self.invalidate()
        self.project_label = _describe(self.ws)
        self.project_id = _project_id(self.ws)
        return self.project_label

    # ------------------------------------------------------------------- cache

    def _cached(self, key: str) -> Any | None:
        if not self.use_cache:
            return None
        return cache.get(self.project_id, key, ttl_hours=self.cache_ttl_hours)

    def _load_defs_from(self, raw_list: Any) -> int:
        n = 0
        for raw in raw_list or []:
            try:
                d = mh.EventDefinition(**raw)
                self._event_defs[d.name] = d
                n += 1
            except Exception:
                continue
        return n

    def _store(self, key: str, value: Any) -> None:
        if self.use_cache:
            cache.put(self.project_id, key, value)

    def cache_note(self) -> str:
        age = cache.age_hours(self.project_id, "event_defs")
        if age is None or not self.use_cache:
            return "live"
        note = "live" if age < 0.02 else f"cached {age:.1f}h ago"
        return f"{note} (stale - rate limited)" if self.stale else note

    def invalidate(self) -> None:
        self._events = None
        self._event_defs.clear()
        self._prop_defs.clear()
        self._volumes = None

    def close(self) -> None:
        try:
            self.ws.close()
        except Exception:  # pragma: no cover - best-effort teardown
            pass

    # -------------------------------------------------------------------- reads

    def projects(self) -> list[dict[str, Any]]:
        out = []
        for p in self.ws.projects():
            out.append({"id": getattr(p, "id", None), "name": getattr(p, "name", str(p))})
        return out

    def event_names(self, *, refresh: bool = False) -> list[str]:
        if self._events is not None and not refresh:
            return self._events
        if not refresh:
            hit = self._cached("events")
            if hit:
                self._events = list(hit)
                return self._events
        try:
            self._events = self.ws.events()
        except mh.RateLimitError as exc:
            stale = cache.get(self.project_id, "events", ttl_hours=-1)
            if stale:
                self._events = list(stale)
                return self._events
            raise RateLimited(exc) from exc
        self._store("events", self._events)
        return self._events

    def event_defs(self, names: Iterable[str] | None = None) -> dict[str, mh.EventDefinition]:
        """Fetch (and cache) Lexicon definitions for the given events."""
        wanted = list(names) if names is not None else self.event_names()

        if not self._event_defs:
            self._load_defs_from(self._cached("event_defs"))

        missing = [n for n in wanted if n not in self._event_defs]
        if missing:
            try:
                for batch in _chunks(missing):
                    for d in self.ws.get_event_definitions(names=batch):
                        self._event_defs[d.name] = d
            except mh.RateLimitError as exc:
                # Out of quota: serve whatever is on disk, however old. Stale
                # definitions are still useful; nothing at all is not.
                if not self._event_defs:
                    self._load_defs_from(
                        cache.get(self.project_id, "event_defs", ttl_hours=-1)
                    )
                    self.stale = True
                if not self._event_defs:
                    raise RateLimited(exc) from exc
                self.stale = True
            else:
                self._store(
                    "event_defs",
                    [d.model_dump(mode="json") for d in self._event_defs.values()],
                )
        return {n: self._event_defs[n] for n in wanted if n in self._event_defs}

    def cached_event_def(self, name: str) -> mh.EventDefinition | None:
        if name not in self._event_defs:
            self.event_defs([name])
        return self._event_defs.get(name)

    def property_defs(
        self, names: Iterable[str], *, resource_type: str = "Event"
    ) -> dict[str, mh.PropertyDefinition]:
        wanted = list(names)
        missing = [n for n in wanted if (resource_type, n) not in self._prop_defs]
        for batch in _chunks(missing):
            for d in self.ws.get_property_definitions(names=batch, resource_type=resource_type):
                self._prop_defs[(resource_type, d.name)] = d
        return {
            n: self._prop_defs[(resource_type, n)]
            for n in wanted
            if (resource_type, n) in self._prop_defs
        }

    def cached_property_def(
        self, name: str, *, resource_type: str = "Event"
    ) -> mh.PropertyDefinition | None:
        key = (resource_type, name)
        if key not in self._prop_defs:
            self.property_defs([name], resource_type=resource_type)
        return self._prop_defs.get(key)

    def volumes(
        self,
        *,
        days: int = 30,
        frm: str | None = None,
        to: str | None = None,
        refresh: bool = False,
    ) -> dict[str, int | None]:
        """Event volume per event name, or None where no source reports it.

        Two sources, because they disagree: `event_counts` takes an explicit
        date range but returns an empty series on some projects, while
        `top_events` always works but only covers today.

        When `frm`/`to` are given (policy `volume_from`/`volume_to`), that
        window is authoritative - use it when the project's data sits outside
        the recent past, which is exactly when today's counts are useless.
        Otherwise top_events leads and a trailing window fills the gaps.

        Anything neither source covers stays None; reporting it as 0 would make
        a dormant event and an unmeasured one look identical.
        """
        window = (frm, to) if frm and to else None
        key = f"volumes-{frm}_{to}" if window else f"volumes-{days}d"

        if self._volumes is not None and self._volume_key == key and not refresh:
            return self._volumes

        if not refresh:
            hit = self._cached(key)
            if hit:
                self._volumes = dict(hit.get("totals", {}))
                self.volume_source = hit.get("source", "cached")
                self._volume_key = key
                return self._volumes

        totals: dict[str, int | None] = {}
        self.volume_source = "none"

        if window:
            totals, ok = self._counts_for(frm, to)
            if ok:
                self.volume_source = f"event_counts {frm}..{to}"
        else:
            try:
                for t in self.ws.top_events(limit=500):
                    if t.count:
                        totals[t.event] = int(t.count)
                if totals:
                    self.volume_source = "top_events (today)"
            except Exception:
                # Volume is a ranking signal, not the point. Losing it degrades
                # the sort order; it must never take the scan down with it.
                pass

            remaining = [n for n in self.event_names() if n not in totals]
            if remaining:
                today = _dt.date.today()
                fallback, ok = self._counts_for(
                    (today - _dt.timedelta(days=days)).isoformat(),
                    today.isoformat(),
                    only=remaining,
                )
                totals.update(fallback)
                if ok:
                    self.volume_source = (
                        f"top_events + event_counts ({days}d)"
                        if self.volume_source != "none"
                        else f"event_counts ({days}d)"
                    )

        if not any(v for v in totals.values()):
            # No source answered. Stale volumes still rank better than nothing.
            stale = cache.get(self.project_id, key, ttl_hours=-1)
            if stale and stale.get("totals"):
                self._volumes = dict(stale["totals"])
                self.volume_source = f"{stale.get('source', 'cached')} [stale]"
                self._volume_key = key
                return self._volumes

        # Explicitly unknown, not zero.
        for name in self.event_names():
            totals.setdefault(name, None)

        self._volumes = totals
        self._volume_key = key
        self._store(key, {"totals": totals, "source": self.volume_source})
        return totals

    def _counts_for(
        self, frm: str, to: str, *, only: list[str] | None = None
    ) -> tuple[dict[str, int], bool]:
        """event_counts summed over a window. Returns (totals, got_any_data)."""
        totals: dict[str, int] = {}
        got = False
        names = only if only is not None else self.event_names()
        for batch in _chunks(names, CHUNK):
            try:
                res = self.ws.event_counts(batch, from_date=frm, to_date=to, unit="month")
            except Exception:
                continue
            for name, series in (res.series or {}).items():
                total = sum(series.values())
                if total:
                    totals[name] = total
                    got = True
        return totals, got

    def tags(self) -> list[dict[str, Any]]:
        return [{"id": t.id, "name": t.name} for t in self.ws.list_lexicon_tags()]


def _project_id(ws: mh.Workspace) -> str:
    project = getattr(getattr(ws, "session", None), "project", None)
    return str(getattr(project, "id", None) or project or "unknown")


def _describe(ws: mh.Workspace) -> str:
    """A short human label for the bound project, e.g. 'Acme Prod (2888997)'."""
    project = getattr(getattr(ws, "session", None), "project", None)
    pid = getattr(project, "id", None) or project
    name = getattr(project, "name", None)

    if not name and pid:
        # The Project object usually carries only an id, and only /me has the
        # display name. But /me is one API call per launch, and on accounts with
        # many projects its response exceeds the SDK's 1MB credential-cache cap,
        # so the SDK cannot cache it and refetches every time (with a scary
        # warning). Cache just the resolved name ourselves - it effectively
        # never changes - so /me is called once per project, not once per run.
        name = cache.get(str(pid), "label", ttl_hours=LABEL_TTL_HOURS)
        if not name:
            try:
                entry = (ws.me().projects or {}).get(str(pid))
                name = getattr(entry, "name", None)
                if name:
                    cache.put(str(pid), "label", name)
            except Exception:
                name = None

    if name and pid:
        return f"{name} ({pid})"
    return f"project {pid}" if pid else "unknown project"


def _main() -> int:
    import argparse
    import sys

    ap = argparse.ArgumentParser(prog="python -m mpgov.session")
    ap.add_argument("--whoami", action="store_true", help="show the active account and project")
    ap.add_argument("--projects", action="store_true", help="list accessible projects")
    ap.add_argument("--project", help="project id to bind to")
    args = ap.parse_args()

    try:
        sess = GovSession.connect(project=args.project)
    except NotAuthenticated as exc:
        print(exc, file=sys.stderr)
        return 1

    if args.projects:
        for p in sess.projects():
            print(f"{p['id']}\t{p['name']}")
    else:
        print(f"Connected: {sess.project_label}")
        print(f"Events in project: {len(sess.event_names())}")
    sess.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
