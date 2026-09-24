"""Exercise the governance logic against the stub - no credentials, no LLM."""

from __future__ import annotations

import json
import pathlib
import tempfile

from mpgov import cache, hygiene, proposals
from mpgov.policy import Policy
from mpgov.render import console, gap_table, summary_panel
from mpgov.session import GovSession
from mpgov.tools import build_server, build_tools
from tests.stub import StubWorkspace

import asyncio


def build(project_id: str = "stub-logic"):
    """A session with a clean cache - these tests assert on live stub reads."""
    cache.clear(project_id)
    ws = StubWorkspace()
    return (
        GovSession(ws=ws, project_label="project 111 (Acme Analytics)", project_id=project_id),
        ws,
    )


def policy(**overrides) -> Policy:
    p = Policy.load()
    p.data = {**p.data, **overrides}
    return p


def test_scan():
    sess, _ = build()
    pol = policy(volume_from=None, volume_to=None)
    rows = hygiene.scan(sess, pol)
    names = [r.event for r in rows]
    assert "$identify" not in names, "ignore_events pattern did not filter Mixpanel internals"
    assert "legacy_ping" not in names, "hidden events should be excluded by default"
    assert names[0] == "Sign Up", f"highest-volume 2-gap event should rank first, got {names}"

    by_name = {r.event: r for r in rows}
    assert by_name["Sign Up"].volume == 412_000, "top_events should supply this"
    assert by_name["Page View"].volume == 3_100_000, "event_counts fallback should supply this"
    assert sess.volume_source.startswith("top_events"), sess.volume_source

    stats = hygiene.summarize(rows)
    assert stats["events_scanned"] == 3
    assert stats["fully_documented"] == 1
    assert stats["missing_by_field"] == {"description": 2, "display_name": 1}
    console.print(summary_panel(sess.project_label, stats, pol.source, sess.volume_source))
    console.print(gap_table(rows))
    print("scan: OK")


def test_explicit_volume_window():
    """An explicit date range must take precedence over today's counts."""
    sess, _ = build()
    pol = policy(volume_from="2025-09-01", volume_to="2025-09-30")
    assert pol.volume_window == ("2025-09-01", "2025-09-30")
    rows = hygiene.scan(sess, pol)
    assert "event_counts 2025-09-01..2025-09-30" == sess.volume_source, sess.volume_source
    # The stub's event_counts answers for every event, including Page View.
    assert all(r.volume is not None for r in rows), [(r.event, r.volume) for r in rows]
    print(f"explicit window: OK ({sess.volume_source})")


def test_unknown_volume_is_not_zero():
    sess, ws = build("stub-novol")

    def no_data(*a, **k):
        raise RuntimeError("no volume endpoint available")

    ws.top_events = no_data
    ws.event_counts = no_data
    rows = hygiene.scan(sess, policy(volume_from=None, volume_to=None))
    assert all(r.volume is None for r in rows), [(r.event, r.volume) for r in rows]
    stats = hygiene.summarize(rows)
    assert stats["pct_of_volume_undocumented"] is None
    assert stats["events_with_unmeasured_volume"] == 3
    print("unknown volume stays None, not 0: OK")


def test_diff():
    sess, _ = build()
    args = {
        "events": [
            {"event": "Sign Up", "description": "A visitor creates an account.", "display_name": "Sign Up"},
            {"event": "Page View", "description": "Fires on every page load."},  # unchanged
        ]
    }
    diffs = proposals.diff_for(sess, args)
    labels = [d.label for d in diffs]
    assert labels == ["event: Sign Up"], f"unchanged fields must not appear as changes: {labels}"
    assert {c[0] for c in diffs[0].changes} == {"description", "display_name"}
    print("diff: OK")


def test_proposal_export():
    sess, _ = build()
    args = {
        "events": [{"event": "Sign Up", "description": "A visitor creates an account."}],
        "properties": [{"property": "email", "sensitive": True, "description": "Registered address."}],
        "note": "plan may be backfilled - confirm with the instrumentation owner.",
    }
    diffs = proposals.diff_for(sess, args)
    with tempfile.TemporaryDirectory() as tmp:
        paths = proposals.save(sess, args, diffs, note=args["note"], out_dir=pathlib.Path(tmp))
        md = pathlib.Path(paths["markdown"]).read_text()
        payload = json.loads(pathlib.Path(paths["json"]).read_text())

    assert "Not applied" in md, "the file must say it has not been applied"
    assert "A visitor creates an account." in md
    assert args["note"] in md, "caveats must reach whoever applies the file"
    assert payload["applied"] is False
    assert payload["project"] == sess.project_label
    entities = {d["entity"] for d in payload["diffs"]}
    assert "event: Sign Up" in entities and "event property: email" in entities, entities
    print("proposal export: OK")


def test_tools_register():
    sess, _ = build()
    pol = Policy.load()
    server = build_server(sess, pol, lambda: "sess-abc")
    tools = build_tools(sess, pol, lambda: "sess-abc")
    assert server["type"] == "sdk" and server["name"] == "mpgov", server
    names = sorted(t.name for t in tools)
    assert names == sorted(
        [
            "list_projects",
            "select_project",
            "scan_lexicon",
            "inspect_event",
            "list_tags",
            "export_lexicon",
            "propose_metadata",
        ]
    ), names
    print("tools:", names)
    print("tools register: OK")


def test_tool_roundtrip():
    """Call the read tools the way the agent would and check the payloads."""
    sess, _ = build()
    tools = {t.name: t for t in build_tools(sess, policy(volume_from=None, volume_to=None), lambda: "s")}

    scan = json.loads(asyncio.run(tools["scan_lexicon"].handler({"limit": 5}))["content"][0]["text"])
    assert scan["summary"]["with_gaps"] == 2
    assert scan["rows"][0]["event"] == "Sign Up"
    assert scan["rows"][0]["volume"] == 412000
    assert "not as zero" in scan["note"], "the agent must be told null != 0"

    ins = json.loads(asyncio.run(tools["inspect_event"].handler({"event": "Sign Up"}))["content"][0]["text"])
    assert ins["volume"] == 412000
    props = {p["property"]: p["sample_values"] for p in ins["properties"]}
    assert props["plan"] == ["free", "pro", "enterprise"], props

    # propose_metadata reports plainly that nothing reached Mixpanel
    import os

    cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            res = json.loads(
                asyncio.run(
                    tools["propose_metadata"].handler(
                        {"events": [{"event": "Sign Up", "description": "New account created."}]}
                    )
                )["content"][0]["text"]
            )
        finally:
            os.chdir(cwd)
    assert res["applied_to_mixpanel"] is False
    assert res["entities"] == ["event: Sign Up"]
    print("tool roundtrip: OK")


if __name__ == "__main__":
    test_scan()
    test_explicit_volume_window()
    test_unknown_volume_is_not_zero()
    test_diff()
    test_proposal_export()
    test_tools_register()
    test_tool_roundtrip()
    for pid in ("stub-logic", "stub-novol"):
        cache.clear(pid)
    print("\nall logic tests passed")
