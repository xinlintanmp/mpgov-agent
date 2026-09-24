"""this file is meant to prevent the agent from writing and making changes to lexicon
Permission gate.

mpgov is read-only against Mixpanel. This callback is the second of the three
enforcement points described in `tools.py`:

* reads are allowed;
* `propose_metadata` shows its diff and asks, because it produces a file you
  will act on and a bad description is worth catching here;
* anything that would mutate Mixpanel is denied outright, whether or not such a
  tool is currently registered.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import tempfile
from typing import Any

from claude_agent_sdk import (
    PermissionResultAllow,
    PermissionResultDeny,
    ToolPermissionContext,
)

from . import proposals
from .render import console, field_diff
from .tools import MUTATING_TOOLS, READ_TOOLS, REVIEW_TOOLS

PROMPT = "[bold]Save these proposals?[/] [green]y[/]es / [red]n[/]o / [cyan]e[/]dit > "

DENIED = (
    "mpgov is read-only and cannot change Mixpanel. Draft the metadata and call "
    "propose_metadata instead, which writes a file the user applies themselves."
)


class Approver:
    """Renders proposal diffs for review and refuses Mixpanel mutations."""

    def __init__(self, session) -> None:
        self.session = session
        self.approve_all = False

    def reset_turn(self) -> None:
        """Called before each user message. 'All' never survives a turn."""
        self.approve_all = False

    # ----------------------------------------------------------------- callback

    async def can_use_tool(
        self, tool_name: str, input_data: dict[str, Any], context: ToolPermissionContext
    ) -> PermissionResultAllow | PermissionResultDeny:
        short = tool_name.rsplit("__", 1)[-1]

        # Deny by name, so reintroducing a mutating tool cannot quietly enable it.
        if short in MUTATING_TOOLS:
            console.print(f"[red]Refused {short}: mpgov does not write to Mixpanel.[/]")
            return PermissionResultDeny(message=DENIED)

        if short in READ_TOOLS:
            return PermissionResultAllow()

        if short not in REVIEW_TOOLS:
            # Unknown tool: refuse rather than assume it is safe.
            return PermissionResultDeny(
                message=f"{short} is not part of mpgov's read-only tool set."
            )

        diffs = proposals.diff_for(self.session, input_data)
        if not diffs:
            return PermissionResultDeny(
                message=(
                    "Nothing would change - Lexicon already holds these values. "
                    "Pick different entities or different fields."
                )
            )

        if self.approve_all:
            return PermissionResultAllow()

        for d in diffs:
            console.print(field_diff(d.label, d.changes))
        if input_data.get("note"):
            console.print(f"[dim]note: {input_data['note']}[/]")

        while True:
            answer = (await _ask(PROMPT)).strip().lower()
            if answer in ("y", "yes", ""):
                return PermissionResultAllow()
            if answer in ("n", "no"):
                return PermissionResultDeny(
                    message="The user rejected these proposals. Ask what was wrong "
                    "before redrafting."
                )
            if answer in ("e", "edit"):
                edited = await _edit(input_data)
                if edited is None:
                    console.print("[dim]Edit discarded.[/]")
                    continue
                for d in proposals.diff_for(self.session, edited):
                    console.print(field_diff(d.label, d.changes))
                confirm = (await _ask("[bold]Save edited version?[/] y/n > ")).strip().lower()
                if confirm in ("y", "yes", ""):
                    return PermissionResultAllow(updated_input=edited)
                continue
            console.print("[dim]Please answer y, n or e.[/]")


async def _ask(prompt: str) -> str:
    return await asyncio.to_thread(console.input, prompt)


async def _edit(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Open the proposals as JSON in $EDITOR and read them back."""

    def run() -> dict[str, Any] | None:
        editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "nano"
        with tempfile.NamedTemporaryFile("w+", suffix=".json", delete=False) as fh:
            json.dump(payload, fh, indent=2)
            path = fh.name
        try:
            subprocess.call([*editor.split(), path])
            with open(path) as fh:
                return json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            console.print(f"[red]Could not read your edit: {exc}[/]")
            return None
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass

    return await asyncio.to_thread(run)
