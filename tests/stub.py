"""A fake Mixpanel Workspace so the logic can be exercised without credentials."""

from __future__ import annotations

import mixpanel_headless as mh

EVENTS = {
    "Sign Up": dict(id=1, description=None, display_name=None, tags=[], verified=False),
    "checkout_completed": dict(id=2, description=None, display_name="Checkout Completed", tags=["revenue"], verified=False),
    "Page View": dict(id=3, description="Fires on every page load.", display_name="Page View", tags=["core"], verified=True),
    "legacy_ping": dict(id=4, description=None, display_name=None, tags=[], verified=False, hidden=True),
    "$identify": dict(id=5, description=None, display_name=None, tags=[], verified=False),
}
VOLUMES = {"Sign Up": 412_000, "checkout_completed": 98_000, "Page View": 3_100_000}
PROPS = {
    "Sign Up": ["plan", "referrer", "email"],
    "checkout_completed": ["amount", "currency"],
}
VALUES = {
    "plan": ["free", "pro", "enterprise"],
    "referrer": ["google.com", "direct", "twitter.com"],
    "email": ["a@b.com"],
    "amount": ["49.00", "199.00"],
    "currency": ["USD", "EUR"],
}


class StubWorkspace:
    def __init__(self):
        self.calls = []

    # -- reads
    def me(self):
        return {"results": {"name": "Acme Analytics"}}

    def projects(self):
        return [type("P", (), {"id": "111", "name": "Acme Prod"})()]

    def events(self, **kw):
        return list(EVENTS)

    def get_event_definitions(self, *, names):
        out = []
        for n in names:
            if n in EVENTS:
                out.append(mh.EventDefinition(name=n, **EVENTS[n]))
        return out

    def get_property_definitions(self, *, names, resource_type=None):
        return [mh.PropertyDefinition(name=n, resource_type=resource_type or "Event") for n in names]

    def properties(self, event):
        return PROPS.get(event, [])

    def property_values(self, property_name, *, event=None, limit=100):
        return VALUES.get(property_name, [])[:limit]

    def event_counts(self, events, *, from_date, to_date, type="general", unit="day"):
        return mh.EventCountsResult(
            events=list(events), from_date=from_date, to_date=to_date, unit=unit, type=type,
            series={e: {from_date: VOLUMES.get(e, 0)} for e in events},
        )

    def top_events(self, *, type="general", limit=None):
        # "Page View" is deliberately absent so the event_counts fallback runs,
        # and "legacy_ping" has no count anywhere -> volume stays unknown.
        rows = [
            mh.TopEvent(event=e, count=c, percent_change=0.0)
            for e, c in VOLUMES.items()
            if e not in ("Page View", "legacy_ping")
        ]
        return rows[:limit] if limit else rows

    def list_lexicon_tags(self):
        return [mh.LexiconTag(id=1, name="core"), mh.LexiconTag(id=2, name="revenue")]

    def export_lexicon(self, *, export_types=None):
        return {"events": list(EVENTS)}

    def event_counts_window(self):
        return None

    # -- writes: intentionally absent. mpgov is read-only; a call to any
    # Mixpanel mutation should blow up loudly in tests.

    def close(self):
        pass
