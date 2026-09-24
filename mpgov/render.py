"""Terminal formatting."""

from __future__ import annotations

from typing import Any, Iterable

from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

console = Console()


def _fmt_volume(n: int | None) -> str:
    if n is None:
        return "?"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1_000:
        return f"{n / 1_000:.1f}K"
    return str(n)


def summary_panel(
    project: str, stats: dict[str, Any], policy_source: str, volume_source: str | None = None
) -> Panel:
    body = Table.grid(padding=(0, 2))
    body.add_column(style="dim")
    body.add_column()
    body.add_row("events scanned", str(stats["events_scanned"]))
    body.add_row(
        "documented",
        f"{stats['fully_documented']} ({stats['coverage_pct']}%)",
    )
    body.add_row("with gaps", f"[yellow]{stats['with_gaps']}[/]")
    missing = ", ".join(f"{k}: {v}" for k, v in sorted(stats["missing_by_field"].items()))
    body.add_row("missing", missing or "-")
    pct = stats.get("pct_of_volume_undocumented")
    body.add_row(
        "undocumented traffic",
        f"{pct}% of events sent" if pct is not None else "[dim]no volume data[/]",
    )
    if volume_source:
        unmeasured = stats.get("events_with_unmeasured_volume") or 0
        suffix = f", {unmeasured} unmeasured" if unmeasured else ""
        body.add_row("volume source", f"[dim]{volume_source}{suffix}[/]")
    body.add_row("untagged / unverified", f"{stats['untagged']} / {stats['unverified']}")
    return Panel(
        body,
        title=f"Lexicon hygiene - {project}",
        subtitle=f"policy: {policy_source}",
        border_style="cyan",
    )


def gap_table(rows: Iterable[Any]) -> Table:
    t = Table(show_lines=False, header_style="bold")
    t.add_column("event", overflow="fold", max_width=44)
    t.add_column("volume", justify="right")
    t.add_column("missing", style="yellow")
    t.add_column("tags", style="dim", max_width=24, overflow="fold")
    t.add_column("ok", justify="center")
    for r in rows:
        t.add_row(
            r.event,
            _fmt_volume(r.volume),
            ", ".join(r.missing) or "-",
            ", ".join(r.tags) or "-",
            "[green]yes[/]" if r.documented else "[red]no[/]",
        )
    return t


def field_diff(entity: str, changes: list[tuple[str, Any, Any]]) -> Panel:
    """Render a before -> after diff for one entity's fields."""
    body = Table.grid(padding=(0, 1))
    body.add_column(style="dim", width=14)
    body.add_column()
    for field_name, before, after in changes:
        body.add_row("", "")
        body.add_row(f"{field_name}", "")
        body.add_row("  before", Text(_show(before), style="red"))
        body.add_row("  after", Text(_show(after), style="green"))
    return Panel(body, title=f"proposed change - {entity}", border_style="yellow")


def _show(value: Any) -> str:
    if value is None:
        return "(empty)"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value) or "(empty)"
    s = str(value)
    return s if s.strip() else "(empty)"


def _short_path(p: str, keep: int = 2) -> str:
    if "/" not in p:
        return p
    parts = p.rstrip("/").split("/")
    return "/".join(parts[-keep:]) if len(parts) > keep else p


def banner(
    project: str, policy_source: str, resumed: bool, cache_note: str | None = None
) -> Panel:
    policy_source = _short_path(policy_source)
    suffix = f" - {cache_note}" if cache_note else ""
    lines = [
        Text.from_markup("[bold]mpgov[/] - Mixpanel Lexicon hygiene agent"),
        Text.from_markup(f"[dim]{project} - policy: {policy_source}{suffix}[/]"),
        Text(""),
        Text.from_markup(
            "Ask things like [cyan]'which high-traffic events have no description?'[/] or "
            "[cyan]'document the top 5 gaps'[/]."
        ),
        Text.from_markup(
            "[dim]Read-only: mpgov never changes Mixpanel. Drafted metadata is shown "
            "as a diff and saved to a file you apply. /help, /exit.[/]"
        ),
    ]
    if resumed:
        lines.append(Text.from_markup("[green]Resumed your previous session.[/]"))
    return Panel(Group(*lines), border_style="cyan")


HELP = r"""
[bold]Commands[/]
  /scan \[n]      run the hygiene scan and print the gap table (default 20 rows)
  /refresh       drop cached Lexicon reads and re-fetch from Mixpanel
  /project <id>  switch Mixpanel project
  /tags          list Lexicon tags
  /proposals     show proposal files drafted for this project
  /cost          show session cost so far
  /help          this message
  /exit          quit

Anything else is sent to the agent.
"""
