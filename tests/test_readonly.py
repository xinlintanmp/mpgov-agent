"""The read-only guarantee. If any of this fails, the guarantee is gone.

Three independent checks, because a single mechanism failing silently is
exactly how a guarantee like this gets lost.
"""

from __future__ import annotations

import asyncio
import pathlib
import re

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from mpgov.approval import Approver
from mpgov.cli import BLOCKED, build_options
from mpgov.policy import Policy
from mpgov.session import GovSession
from mpgov.tools import MUTATING_TOOLS, READ_TOOLS, REVIEW_TOOLS, build_tools, qualified
from tests.stub import StubWorkspace

# Every Mixpanel Headless method that changes server-side state.
SDK_WRITE_METHODS = [
    "update_event_definition",
    "update_property_definition",
    "bulk_update_event_definitions",
    "bulk_update_property_definitions",
    "create_lexicon_tag",
    "delete_lexicon_tag",
    "update_lexicon_tag",
    "create_drop_filter",
    "update_drop_filter",
    "delete_drop_filter",
    "delete_event_definition",
    "init_schema_enforcement",
    "update_schema_enforcement",
    "replace_schema_enforcement",
    "delete_schema_enforcement",
    "create_schema",
    "update_schema",
    "create_schemas_bulk",
    "update_schemas_bulk",
    "delete_schemas",
    "create_custom_property",
    "update_custom_property",
    "delete_custom_property",
    "create_cohort",
    "update_cohort",
    "delete_cohort",
    "upload_lookup_table",
    "update_lookup_table",
    "delete_lookup_tables",
    "create_deletion_request",
    "set_business_context",
    "update_business_context",
]


def build():
    return GovSession(ws=StubWorkspace(), project_label="stub", project_id="stub-ro")


def test_no_mutating_tool_registered():
    tools = build_tools(build(), Policy.load(), lambda: "ro")
    names = {t.name for t in tools}
    assert not (names & set(MUTATING_TOOLS)), f"mutating tools registered: {names & set(MUTATING_TOOLS)}"
    assert names == set(READ_TOOLS) | set(REVIEW_TOOLS), names
    print(f"registered tools are read-only ({len(names)}): OK")


def test_source_never_calls_a_mixpanel_write():
    """Static check: no module references an SDK write method."""
    offenders = []
    for path in sorted(pathlib.Path("mpgov").glob("*.py")):
        src = path.read_text()
        for method in SDK_WRITE_METHODS:
            # Skip the deny-list in tools.py, which names them as strings only.
            for m in re.finditer(rf"\.{method}\s*\(", src):
                offenders.append(f"{path.name}: .{method}()")
    assert not offenders, "source calls Mixpanel write methods:\n  " + "\n  ".join(offenders)
    print(f"no SDK write call in any module ({len(SDK_WRITE_METHODS)} checked): OK")


def test_gate_denies_mutating_tool_by_name():
    """Even if a mutating tool reappeared, the gate refuses it."""
    a = Approver(build())
    for name in MUTATING_TOOLS:
        r = asyncio.run(a.can_use_tool(f"mcp__mpgov__{name}", {"events": []}, None))
        assert isinstance(r, PermissionResultDeny), f"{name} was not denied"
        assert "read-only" in r.message
    print(f"gate denies all {len(MUTATING_TOOLS)} mutating names: OK")


def test_gate_denies_unknown_tool():
    a = Approver(build())
    r = asyncio.run(a.can_use_tool("mcp__mpgov__something_new", {}, None))
    assert isinstance(r, PermissionResultDeny), r
    print("gate denies unknown tools: OK")


def test_reads_allowed_without_prompting():
    a = Approver(build())
    for name in READ_TOOLS:
        r = asyncio.run(a.can_use_tool(f"mcp__mpgov__{name}", {}, None))
        assert isinstance(r, PermissionResultAllow), f"{name} should be pre-approved"
    print("reads auto-allowed: OK")


def test_review_tool_is_not_pre_approved():
    """propose_metadata must fall through to the callback, not be auto-approved."""
    allowed = set(qualified(READ_TOOLS))
    for name in REVIEW_TOOLS:
        q = f"mcp__mpgov__{name}"
        assert q not in allowed, (
            f"{name} is in allowed_tools, which auto-approves it BEFORE can_use_tool "
            "runs - the review diff would never be shown"
        )
    opts = build_options(build(), Policy.load(), Approver(build()).can_use_tool, lambda: "ro")
    assert set(opts.allowed_tools) == allowed, opts.allowed_tools
    assert opts.can_use_tool is not None
    print("review tool falls through to the gate: OK")


def test_filesystem_and_shell_blocked():
    opts = build_options(build(), Policy.load(), Approver(build()).can_use_tool, lambda: "ro")
    for t in ("Bash", "Read", "Write", "Edit", "WebFetch"):
        assert t in opts.disallowed_tools, f"{t} not blocked"
    assert set(BLOCKED) <= set(opts.disallowed_tools)
    print("filesystem/shell/web built-ins blocked: OK")


def test_live_tool_surface_is_only_mpgov():
    """Spawn the real agent session and assert what it can actually see.

    Static config is not proof. Probing the live session is how the 21
    unblocked built-ins (ToolSearch, Skill, Workflow, CronCreate, ...) were
    found in the first place.
    """
    import asyncio as aio

    from claude_agent_sdk import ClaudeSDKClient, ResultMessage, SystemMessage

    async def probe():
        sess = build()
        opts = build_options(sess, Policy.load(), Approver(sess).can_use_tool, lambda: "probe")
        async with ClaudeSDKClient(options=opts) as c:
            await c.query("Reply with just: ok")
            async for m in c.receive_response():
                if isinstance(m, SystemMessage) and m.subtype == "init":
                    return m.data or {}
                if isinstance(m, ResultMessage):
                    break
        return {}

    data = aio.run(probe())
    tools = set(data.get("tools") or [])
    assert tools, "could not read the live tool list"

    expected = set(qualified(READ_TOOLS)) | set(qualified(REVIEW_TOOLS))
    assert tools == expected, (
        f"unexpected tools in the agent's session:\n"
        f"  extra:   {sorted(tools - expected)}\n"
        f"  missing: {sorted(expected - tools)}"
    )

    servers = {s.get("name") for s in (data.get("mcp_servers") or [])}
    assert servers == {"mpgov"}, f"other MCP servers visible: {servers - {'mpgov'}}"
    print(f"live surface is exactly the {len(tools)} mpgov tools, 1 server: OK")


if __name__ == "__main__":
    test_no_mutating_tool_registered()
    test_source_never_calls_a_mixpanel_write()
    test_gate_denies_mutating_tool_by_name()
    test_gate_denies_unknown_tool()
    test_reads_allowed_without_prompting()
    test_review_tool_is_not_pre_approved()
    test_filesystem_and_shell_blocked()
    test_live_tool_surface_is_only_mpgov()
    print("\nread-only guarantee holds")
