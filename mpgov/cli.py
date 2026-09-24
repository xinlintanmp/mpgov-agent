"""The interactive terminal loop."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import warnings
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    CanUseToolShadowedWarning,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ThinkingBlock,
    ToolUseBlock,
)
from rich.table import Table

from . import cache, hygiene, prompts, proposals
from .approval import Approver
from .policy import Policy
from .render import HELP, banner, console, gap_table, summary_panel
from .session import GovSession, NotAuthenticated, RateLimited
from .tools import READ_TOOLS, SERVER_NAME, build_server, qualified

STATE = Path.home() / ".mpgov" / "state.json"

# Read tools are pre-approved on purpose, so the SDK's warning about them
# shadowing can_use_tool is expected. Write tools are deliberately absent from
# allowed_tools, which is what keeps the gate in front of every mutation.
warnings.filterwarnings("ignore", category=CanUseToolShadowedWarning)

# The agent's only capability is Mixpanel reads. Built-ins are removed with an
# ALLOWLIST (`tools=[]`), not a denylist: probing the live session showed 21
# built-ins beyond the obvious ones - ToolSearch, Skill, Workflow, CronCreate,
# SendMessage and friends - and a denylist silently misses whatever is added
# next. BLOCKED stays as a second layer naming the ones that matter most.
BLOCKED = [
    "Bash",
    "Read",
    "Write",
    "Edit",
    "NotebookEdit",
    "Glob",
    "Grep",
    "WebSearch",
    "WebFetch",
    "Task",
    "TodoWrite",
    "ToolSearch",  # could surface deferred tools, including other servers' writes
    "Skill",
    "Workflow",
    "SendMessage",
    "RemoteTrigger",
    "PushNotification",
    "CronCreate",
    "CronDelete",
    "CronList",
    "Monitor",
    "ShareOnboardingGuide",
]


def _load_state() -> dict[str, Any]:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_state(**kv: Any) -> None:
    STATE.parent.mkdir(parents=True, exist_ok=True)
    state = _load_state()
    state.update(kv)
    STATE.write_text(json.dumps(state, indent=2))


# mpgov spawns the Claude Code CLI, which picks up its auth from the
# environment. A shell profile exporting CLAUDE_CODE_USE_VERTEX routes the agent
# through Vertex AI, which fails if the Google credentials lack
# aiplatform.endpoints.predict on that GCP project - an error that looks like an
# mpgov bug but is not. mpgov therefore clears those vars for its own subprocess
# by default, leaving the shell (and every other Claude Code session) untouched.
# Pass use_vertex=True / --vertex to inherit them.
VERTEX_VARS = ("CLAUDE_CODE_USE_VERTEX", "ANTHROPIC_VERTEX_PROJECT_ID", "CLOUD_ML_REGION")


def build_options(
    session: GovSession,
    policy: Policy,
    can_use_tool: Any,
    session_id: Any,
    *,
    model: str | None = None,
    resume: str | None = None,
    max_turns: int | None = None,
    use_vertex: bool = False,
) -> ClaudeAgentOptions:
    """The one place agent options are defined. Tests use this too."""
    return ClaudeAgentOptions(
        system_prompt=prompts.build(policy),
        mcp_servers={SERVER_NAME: build_server(session, policy, session_id)},
        # Reads only. propose_metadata is deliberately absent so it falls
        # through to can_use_tool for review - an allowed_tools entry
        # auto-approves the call *before* the callback runs.
        allowed_tools=qualified(READ_TOOLS),
        # No built-in tools at all; only the mpgov MCP server's.
        tools=[],
        # Ignore file-based MCP config so no other server (and no other
        # project's write tools) can appear in this agent's session.
        strict_mcp_config=True,
        disallowed_tools=BLOCKED,
        permission_mode="default",
        can_use_tool=can_use_tool,
        model=model,
        resume=resume,
        max_turns=max_turns,
        cwd=str(Path.cwd()),
        setting_sources=[],
        env={} if use_vertex else {v: "" for v in VERTEX_VARS},
    )


class Repl:
    def __init__(self, session: GovSession, policy: Policy, args: argparse.Namespace) -> None:
        self.session = session
        self.policy = policy
        self.args = args
        self.session_id: str | None = None
        self.cost = 0.0
        self.approver = Approver(session)

    # ------------------------------------------------------------ local commands

    def handle_command(self, line: str) -> bool:
        """Returns True if the line was a local command (not sent to the agent)."""
        parts = line.split()
        cmd, rest = parts[0], parts[1:]

        if cmd in ("/exit", "/quit"):
            raise EOFError
        if cmd == "/help":
            console.print(HELP)
            return True
        if cmd == "/refresh":
            n = cache.clear(self.session.project_id)
            self.session.invalidate()
            console.print(f"[green]Cache cleared ({n} files). Next read hits Mixpanel.[/]")
            return True
        if cmd == "/scan":
            limit = int(rest[0]) if rest and rest[0].isdigit() else 20
            try:
                rows = hygiene.scan(self.session, self.policy)
            except RateLimited as exc:
                console.print(f"[yellow]{exc}[/]")
                return True
            console.print(
                summary_panel(
                    self.session.project_label,
                    hygiene.summarize(rows),
                    self.policy.source,
                    self.session.volume_source,
                )
            )
            console.print(gap_table(rows[:limit]))
            return True
        if cmd == "/project":
            if not rest:
                for p in self.session.projects():
                    console.print(f"  {p['id']}\t{p['name']}")
            else:
                label = self.session.switch_project(rest[0])
                console.print(f"[green]Switched to {label}[/]")
            return True
        if cmd == "/tags":
            for t in self.session.tags():
                console.print(f"  {t['id']}\t{t['name']}")
            return True
        if cmd == "/proposals":
            rows = proposals.read_log(limit=30, project=self.session.project_label)
            if not rows:
                console.print(
                    f"[dim]No proposals drafted for {self.session.project_label} yet.[/]"
                )
                return True
            t = Table(header_style="bold")
            t.add_column("when", style="dim")
            t.add_column("entities", overflow="fold", max_width=60)
            for r in rows:
                t.add_row(r["ts"][:19], ", ".join(r.get("entities", []))[:200])
            console.print(t)
            console.print(f"[dim]Log: {proposals.PROPOSAL_LOG}[/]")
            return True
        if cmd == "/cost":
            console.print(f"Session cost so far: ${self.cost:.4f}")
            return True

        console.print(f"[yellow]Unknown command {cmd}. /help for the list.[/]")
        return True

    # -------------------------------------------------------------------- render

    def render(self, message: Any) -> None:
        if isinstance(message, AssistantMessage):
            for block in message.content:
                if isinstance(block, TextBlock):
                    console.print(block.text)
                elif isinstance(block, ThinkingBlock):
                    continue
                elif isinstance(block, ToolUseBlock):
                    console.print(f"[dim]  · {block.name.rsplit('__', 1)[-1]}[/]")
        elif isinstance(message, SystemMessage):
            if message.subtype == "init":
                sid = (message.data or {}).get("session_id")
                if sid:
                    self.session_id = sid
                    _save_state(session_id=sid)
        elif isinstance(message, ResultMessage):
            if message.session_id:
                self.session_id = message.session_id
                _save_state(session_id=message.session_id)
            if message.total_cost_usd:
                self.cost = message.total_cost_usd
                console.print(f"[dim]  ${message.total_cost_usd:.4f}[/]")
            if message.is_error:
                console.print(f"[red]Agent error: {message.subtype}[/]")

    # ---------------------------------------------------------------------- loop

    async def run(self) -> int:
        resume_id = _load_state().get("session_id") if self.args.resume else None
        options = build_options(
            self.session,
            self.policy,
            self.approver.can_use_tool,
            lambda: self.session_id,
            model=self.args.model,
            resume=resume_id,
            use_vertex=bool(getattr(self.args, "vertex", False)),
        )

        console.print(
            banner(
                self.session.project_label,
                self.policy.source,
                bool(resume_id),
                self.session.cache_note(),
            )
        )

        async with ClaudeSDKClient(options=options) as client:
            while True:
                try:
                    line = (await asyncio.to_thread(console.input, "\n[bold cyan]you >[/] ")).strip()
                except (EOFError, KeyboardInterrupt):
                    break
                if not line:
                    continue
                if line.startswith("/"):
                    try:
                        self.handle_command(line)
                    except EOFError:
                        break
                    continue

                self.approver.reset_turn()
                console.print()
                try:
                    await client.query(line)
                    async for message in client.receive_response():
                        self.render(message)
                except KeyboardInterrupt:
                    await client.interrupt()
                    console.print("[yellow]Interrupted.[/]")

        console.print("\n[dim]Bye. Resume with `mpgov --resume`.[/]")
        return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="mpgov", description="Mixpanel Lexicon hygiene agent")
    ap.add_argument("--project", help="Mixpanel project id")
    ap.add_argument("--policy", help="path to policy.yaml")
    ap.add_argument("--resume", action="store_true", help="continue the previous conversation")
    ap.add_argument("--model", help="model override, e.g. claude-opus-5")
    ap.add_argument(
        "--vertex",
        action="store_true",
        help="route the agent through Vertex AI (inherits CLAUDE_CODE_USE_VERTEX from the "
        "shell); off by default so a shell-level Vertex config cannot break mpgov",
    )
    ap.add_argument("--refresh", action="store_true", help="ignore cached Lexicon reads")
    ap.add_argument(
        "--cache-ttl", type=float, default=None, help="cache lifetime in hours (default 6)"
    )
    args = ap.parse_args()

    policy = Policy.load(args.policy)
    try:
        session = GovSession.connect(project=args.project)
    except NotAuthenticated as exc:
        console.print(f"[red]{exc}[/]")
        return 1

    if args.cache_ttl is not None:
        session.cache_ttl_hours = args.cache_ttl
    if args.refresh:
        cache.clear(session.project_id)

    repl = Repl(session, policy, args)
    try:
        return asyncio.run(repl.run())
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
