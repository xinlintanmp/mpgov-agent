"""Live end-to-end against the real Mixpanel project.

mpgov cannot write to Mixpanel; this additionally refuses the proposal file so
the run leaves nothing at all behind.

This is the real verification: does the agent produce useful, grounded
governance output on actual Lexicon data?
"""

from __future__ import annotations

import asyncio
import sys

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeSDKClient,
    PermissionResultDeny,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)

from mpgov import approval
from mpgov.cli import build_options
from mpgov.policy import Policy
from mpgov.session import GovSession

CALLS: list[str] = []
BLOCKED_WRITES: list[str] = []


class ReadOnly(approval.Approver):
    """Also refuses the proposal file, so this run writes nothing anywhere."""

    async def can_use_tool(self, tool_name, input_data, context):
        short = tool_name.rsplit("__", 1)[-1]
        if short in approval.REVIEW_TOOLS:
            BLOCKED_WRITES.append(short)
            print(f"\033[1;33m  [BLOCKED {short} - nothing written in this test]\033[0m")
            return PermissionResultDeny(
                message="Do not save proposals in this session; show the drafted copy "
                "in your reply instead."
            )
        from claude_agent_sdk import PermissionResultAllow

        return PermissionResultAllow()


async def turn(client: ClaudeSDKClient, text: str) -> None:
    print(f"\n\033[1;36myou >\033[0m {text}\n")
    async for _ in _drain(client, text):
        pass


async def _drain(client: ClaudeSDKClient, text: str):
    await client.query(text)
    async for msg in client.receive_response():
        if isinstance(msg, AssistantMessage):
            for b in msg.content:
                if isinstance(b, TextBlock):
                    print(b.text)
                elif isinstance(b, ToolUseBlock):
                    short = b.name.rsplit("__", 1)[-1]
                    CALLS.append(short)
                    print(f"\033[2m  · {short} {str(b.input)[:100]}\033[0m")
        elif isinstance(msg, ResultMessage):
            print(f"\033[2m  ${msg.total_cost_usd or 0:.4f} · {msg.num_turns} turns\033[0m")
        yield msg


async def main() -> int:
    session = GovSession.connect()
    print(f"Live project: {session.project_label}")
    options = build_options(
        session, Policy.load(), ReadOnly(session).can_use_tool, lambda: "live-ro", max_turns=20
    )

    async with ClaudeSDKClient(options=options) as client:
        await turn(client, "Give me the state of Lexicon hygiene in this project.")
        await turn(
            client,
            "Take the single highest-volume event with no description. Inspect it and show me "
            "the description you would draft, and why.",
        )

    print("\n" + "=" * 72)
    print(f"tools used: {sorted(set(CALLS))}")
    print(f"proposal saves blocked: {len(BLOCKED_WRITES)}")
    failures = []
    if "scan_lexicon" not in CALLS:
        failures.append("never scanned")
    if "inspect_event" not in CALLS:
        failures.append("GROUNDING VIOLATION: proposed a description without inspecting")
    if failures:
        print("\n\033[1;31mFAILURES\033[0m")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\n\033[1;32mlive read-only test passed\033[0m")
    session.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
