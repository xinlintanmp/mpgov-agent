"""The disk cache: hits, TTL expiry, stale fallback under rate limiting."""

from __future__ import annotations

import mixpanel_headless as mh

from mpgov import cache, hygiene
from mpgov.policy import Policy
from mpgov.session import GovSession, RateLimited
from tests.stub import StubWorkspace

PROJECT = "test-cache-project"


class Counting(StubWorkspace):
    """Counts API hits and can be switched to rate-limiting."""

    def __init__(self):
        super().__init__()
        self.reads = 0
        self.limited = False

    def _guard(self):
        if self.limited:
            raise mh.RateLimitError("Rate limit exceeded after max retries. Retry after 3600 seconds.")
        self.reads += 1

    def events(self, **kw):
        self._guard()
        return super().events(**kw)

    def get_event_definitions(self, *, names):
        self._guard()
        return super().get_event_definitions(names=names)

    def top_events(self, **kw):
        self._guard()
        return super().top_events(**kw)


def fresh(ws) -> GovSession:
    return GovSession(ws=ws, project_label="stub", project_id=PROJECT)


def test_cache_avoids_refetch():
    cache.clear(PROJECT)
    policy = Policy.load()

    ws = Counting()
    hygiene.scan(fresh(ws), policy)
    first = ws.reads
    assert first > 0

    # A brand-new session for the same project must not hit the API again.
    ws2 = Counting()
    rows = hygiene.scan(fresh(ws2), policy)
    assert ws2.reads == 0, f"cache miss: {ws2.reads} API reads on second session"
    assert len(rows) == 3, rows
    assert rows[0].event == "Sign Up", "cached rows must keep the volume ranking"
    print(f"cache hit: {first} reads cold, {ws2.reads} warm - OK")


def test_ttl_expiry():
    cache.clear(PROJECT)
    ws = Counting()
    hygiene.scan(fresh(ws), Policy.load())

    ws2 = Counting()
    s = fresh(ws2)
    s.cache_ttl_hours = 0  # everything is immediately stale
    hygiene.scan(s, Policy.load())
    assert ws2.reads > 0, "expired cache should have been re-fetched"
    print("ttl expiry: OK")


def test_stale_cache_survives_rate_limit():
    """An exhausted quota must fall back to stale data, not fail."""
    cache.clear(PROJECT)
    hygiene.scan(fresh(Counting()), Policy.load())

    ws = Counting()
    ws.limited = True
    s = fresh(ws)
    s.cache_ttl_hours = 0  # force a refetch attempt, which will be rate limited
    rows = hygiene.scan(s, Policy.load())
    assert len(rows) == 3, f"stale fallback failed: {rows}"
    print("stale fallback under rate limit: OK")


def test_rate_limit_with_no_cache_raises_cleanly():
    cache.clear(PROJECT)
    ws = Counting()
    ws.limited = True
    try:
        hygiene.scan(fresh(ws), Policy.load())
    except RateLimited as exc:
        assert exc.retry_after == 3600, exc.retry_after
        assert "60 minutes" in str(exc), str(exc)
        print("clean RateLimited with retry-after: OK")
        return
    raise AssertionError("expected RateLimited")


if __name__ == "__main__":
    test_cache_avoids_refetch()
    test_ttl_expiry()
    test_stale_cache_survives_rate_limit()
    test_rate_limit_with_no_cache_raises_cleanly()
    cache.clear(PROJECT)
    print("\nall cache tests passed")
