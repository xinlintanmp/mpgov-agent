"""Exercise the review gate with scripted keystrokes."""

from __future__ import annotations

import asyncio

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny

from mpgov import approval
from mpgov.session import GovSession
from tests.stub import StubWorkspace

CTX = None  # the callback never touches the context

PROPOSE = "mcp__mpgov__propose_metadata"
ARGS = {"events": [{"event": "Sign Up", "description": "A visitor creates an account."}]}


def gate(answers: list[str]) -> approval.Approver:
    it = iter(answers)

    async def fake_ask(prompt: str) -> str:
        return next(it)

    approval._ask = fake_ask  # type: ignore[assignment]
    return approval.Approver(
        GovSession(ws=StubWorkspace(), project_label="stub", project_id="stub-approval")
    )


def test_read_tools_auto_allow():
    a = gate([])
    r = asyncio.run(a.can_use_tool("mcp__mpgov__scan_lexicon", {}, CTX))
    assert isinstance(r, PermissionResultAllow), r
    print("read auto-allow: OK")


def test_yes():
    a = gate(["y"])
    r = asyncio.run(a.can_use_tool(PROPOSE, ARGS, CTX))
    assert isinstance(r, PermissionResultAllow), r
    assert r.updated_input is None
    print("approve: OK")


def test_no_denies_with_reason():
    a = gate(["n"])
    r = asyncio.run(a.can_use_tool(PROPOSE, ARGS, CTX))
    assert isinstance(r, PermissionResultDeny), r
    assert "rejected" in r.message
    print("deny: OK")


def test_approve_all_scoped_to_turn():
    a = gate(["y"])
    assert isinstance(asyncio.run(a.can_use_tool(PROPOSE, ARGS, CTX)), PermissionResultAllow)
    a.approve_all = True
    # No keystroke available; if the gate prompted, next() would raise.
    assert isinstance(asyncio.run(a.can_use_tool(PROPOSE, ARGS, CTX)), PermissionResultAllow)
    a.reset_turn()
    assert a.approve_all is False, "'all' must not survive the turn"
    print("approve-all scoped to turn: OK")


def test_noop_proposal_is_denied():
    """A proposal that would change nothing should not produce a file."""
    a = gate([])
    r = asyncio.run(
        a.can_use_tool(
            PROPOSE,
            {"events": [{"event": "Page View", "description": "Fires on every page load."}]},
            CTX,
        )
    )
    assert isinstance(r, PermissionResultDeny), r
    assert "already holds" in r.message
    print("no-op proposal denied: OK")


def test_bad_answer_reprompts():
    a = gate(["huh?", "y"])
    assert isinstance(asyncio.run(a.can_use_tool(PROPOSE, ARGS, CTX)), PermissionResultAllow)
    print("reprompt on bad input: OK")


def test_edit_returns_updated_input():
    edited = {"events": [{"event": "Sign Up", "description": "Edited by the human."}]}

    async def fake_edit(payload):
        return edited

    approval._edit = fake_edit  # type: ignore[assignment]
    a = gate(["e", "y"])
    r = asyncio.run(a.can_use_tool(PROPOSE, ARGS, CTX))
    assert isinstance(r, PermissionResultAllow), r
    assert r.updated_input == edited, r.updated_input
    print("edit path: OK")


def test_mutation_denied_without_prompting():
    """No keystroke is offered for a Mixpanel write - it is simply refused."""
    a = gate([])
    r = asyncio.run(a.can_use_tool("mcp__mpgov__document_events", ARGS, CTX))
    assert isinstance(r, PermissionResultDeny), r
    assert "read-only" in r.message
    print("mutation denied unprompted: OK")


if __name__ == "__main__":
    test_read_tools_auto_allow()
    test_yes()
    test_no_denies_with_reason()
    test_approve_all_scoped_to_turn()
    test_noop_proposal_is_denied()
    test_bad_answer_reprompts()
    test_edit_returns_updated_input()
    test_mutation_denied_without_prompting()
    print("\nall approval tests passed")
