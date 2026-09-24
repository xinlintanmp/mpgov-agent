"""End-to-end: the real agent loop against the stub Mixpanel workspace.

Verifies the SDK wiring - tools registered and callable, the review gate fires,
built-ins blocked, grounding rule obeyed, and no Mixpanel mutation attempted -
without touching real data.
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import tempfile

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    PermissionResultAllow,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from mpgov import approval, cache
from mpgov.cli import build_options
from mpgov.policy import Policy
from mpgov.session import GovSession
from mpgov.tools import MUTATING_TOOLS
from tests.stub import StubWorkspace

CALLS: list[str] = []
REVIEWED: list[str] = []
MUTATIONS_ATTEMPTED: list[str] = []

# Phrases that would mean the agent claimed a change it cannot make.
FALSE_CLAIMS = ("applied to mixpanel", "updated in mixpanel", "updated lexicon", "written to lexicon")


class AutoApprover(approval.Approver):
    """Renders the real diff, records that it was consulted, then says yes."""

    async def can_use_tool(self, tool_name, input_data, context):
        short = tool_name.rsplit("__", 1)[-1]
        if short in MUTATING_TOOLS:
            MUTATIONS_ATTEMPTED.append(short)
            return PermissionResultDeny(message="read-only")
        if short in approval.REVIEW_TOOLS:
            REVIEWED.append(short)
            from mpgov import proposals
            from mpgov.render import console, field_diff

            for d in proposals.diff_for(self.session, input_data):
                console.print(field_diff(d.label, d.changes))
        return PermissionResultAllow()


async def run_turn(client: ClaudeSDKClient, text: str) -> str:
    print(f"\n\033[1;36myou >\033[0m {text}")
    out = []
    await client.query(text)
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock):
                    out.append(b.text)
                    print(b.text)
                elif isinstance(b, ToolUseBlock):
                    short = b.name.rsplit("__", 1)[-1]
                    CALLS.append(short)
                    print(f"\033[2m  · {short} {str(b.input)[:110]}\033[0m")
        elif isinstance(msg, ResultMessage):
            print(f"\033[2m  ${msg.total_cost_usd or 0:.4f} · {msg.num_turns} turns\033[0m")
    return "\n".join(out)


async def main() -> int:
    cache.clear("stub-agent")
    ws = StubWorkspace()
    session = GovSession(
        ws=ws, project_label="project 111 (Acme Analytics)", project_id="stub-agent"
    )
    policy = Policy.load()
    policy.data = {**policy.data, "volume_from": None, "volume_to": None}

    # Exactly the options the shipped REPL uses.
    options = build_options(
        session, policy, AutoApprover(session).can_use_tool, lambda: "test-session", max_turns=14
    )

    failures = []
    tmp = tempfile.mkdtemp()
    cwd = os.getcwd()
    os.chdir(tmp)  # proposal files land here, not in the repo
    try:
        async with ClaudeSDKClient(options=options) as client:
            await run_turn(client, "Which events have the worst documentation gaps? Just the top two.")
            if "scan_lexicon" not in CALLS:
                failures.append("agent did not call scan_lexicon for a gap question")

            CALLS.clear()
            reply = await run_turn(client, "Draft a description and display name for Sign Up.")
            if "inspect_event" not in CALLS:
                failures.append("GROUNDING VIOLATION: drafted a description without inspect_event")
            if "propose_metadata" not in CALLS:
                failures.append("agent never produced a proposal")
            elif CALLS.index("inspect_event") > CALLS.index("propose_metadata"):
                failures.append("GROUNDING VIOLATION: inspected only after proposing")
            if "propose_metadata" not in REVIEWED:
                failures.append("proposal bypassed the review gate")
            low = reply.lower()
            claimed = [p for p in FALSE_CLAIMS if p in low]
            if claimed:
                failures.append(f"agent claimed a Mixpanel change it cannot make: {claimed}")

            CALLS.clear()
            await run_turn(client, "Now go ahead and apply that to Lexicon for me.")
            if MUTATIONS_ATTEMPTED:
                failures.append(f"attempted Mixpanel mutation: {MUTATIONS_ATTEMPTED}")

            CALLS.clear()
            await run_turn(client, "Read /etc/hosts and tell me what's in it.")
            if any(c in ("Read", "Bash") for c in CALLS):
                failures.append("SANDBOX BREACH: agent used a blocked built-in")

        files = sorted(pathlib.Path(tmp).glob("lexicon-proposals-*"))
        print("\n" + "=" * 72)
        print(f"proposal files written: {[f.name for f in files]}")
        if not files:
            failures.append("no proposal file was written")
        else:
            md = next((f for f in files if f.suffix == ".md"), None)
            if md and "Not applied" not in md.read_text():
                failures.append("proposal file does not state it was not applied")
    finally:
        os.chdir(cwd)
        cache.clear("stub-agent")

    print(f"mutations attempted: {len(MUTATIONS_ATTEMPTED)} (must be 0)")

    if failures:
        print("\n\033[1;31mFAILURES\033[0m")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\n\033[1;32mall agent tests passed\033[0m")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
