"""The agent's entire vocabulary: typed tools over the Mixpanel Headless SDK.

**All Mixpanel access here is read-only.** No tool in this module calls a
Mixpanel write endpoint. The agent drafts metadata and exports it for you to
apply; it cannot change Lexicon itself.

Three independent things enforce that, because one of them failing silently is
exactly how this kind of guarantee gets lost:

1. No mutating tool is defined or registered (this file).
2. `MUTATING_TOOLS` names the Mixpanel write operations, and `approval.py`
   denies any call matching them - so re-adding one does not quietly grant it.
3. Filesystem, shell and web built-ins are blocked in `cli.BLOCKED`.
"""

from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

from claude_agent_sdk import create_sdk_mcp_server, tool

from . import hygiene, proposals
from .policy import Policy
from .session import GovSession, RateLimited

SERVER_NAME = "mpgov"

# Pre-approved: every one of these is a read.
READ_TOOLS = (
    "list_projects",
    "select_project",
    "scan_lexicon",
    "inspect_event",
    "list_tags",
    "export_lexicon",
)

# Writes a local file, nothing in Mixpanel. Gated so the diff is reviewed first.
REVIEW_TOOLS = ("propose_metadata",)

# Mixpanel mutations. Deliberately NOT implemented and NOT registered. Named
# here so approval.py can refuse them outright if one is ever reintroduced -
# and note that listing a tool in allowed_tools auto-approves it *before*
# can_use_tool runs, so nothing mutating may ever appear in READ_TOOLS.
MUTATING_TOOLS = (
    "document_events",
    "document_properties",
    "bulk_tag_events",
    "create_tag",
    "update_event_definition",
    "update_property_definition",
    "bulk_update_event_definitions",
    "bulk_update_property_definitions",
    "create_lexicon_tag",
    "create_drop_filter",
    "update_schema_enforcement",
)


def qualified(names) -> list[str]:
    """Tool names as the permission system sees them."""
    return [f"mcp__{SERVER_NAME}__{n}" for n in names]


def _ok(payload: Any) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, indent=2, default=str)}]}


def _err(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


def build_tools(
    session: GovSession,
    policy: Policy,
    session_id: Callable[[], str | None],
) -> list[Any]:
    """Every tool the agent gets, bound to this Mixpanel session."""

    # ------------------------------------------------------------------ reads

    @tool(
        "list_projects",
        "List the Mixpanel projects this account can access.",
        {"type": "object", "properties": {}},
    )
    async def list_projects(args: dict[str, Any]) -> dict[str, Any]:
        return _ok({"active": session.project_label, "projects": session.projects()})

    @tool(
        "select_project",
        "Switch to a different Mixpanel project. Clears all cached Lexicon data.",
        {
            "type": "object",
            "properties": {"project": {"type": "string", "description": "Project id"}},
            "required": ["project"],
        },
    )
    async def select_project(args: dict[str, Any]) -> dict[str, Any]:
        label = session.switch_project(str(args["project"]))
        return _ok({"active": label, "events": len(session.event_names())})

    @tool(
        "scan_lexicon",
        "Scan the project's Lexicon for documentation gaps. Returns a summary plus the "
        "highest-impact gap rows, ranked by event volume. Start here.",
        {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "Gap rows to return (default 25)"},
                "only_missing": {
                    "type": "boolean",
                    "description": "Only return events with gaps (default true)",
                },
                "include_hidden": {"type": "boolean", "description": "Include hidden events"},
                "field": {
                    "type": "string",
                    "description": "Only events missing this specific field, e.g. 'description'",
                },
            },
        },
    )
    async def scan_lexicon(args: dict[str, Any]) -> dict[str, Any]:
        limit = int(args.get("limit") or 25)
        try:
            rows = hygiene.scan(session, policy, include_hidden=bool(args.get("include_hidden")))
        except RateLimited as exc:
            return _err(str(exc))
        stats = hygiene.summarize(rows)

        selected = rows
        if args.get("field"):
            selected = [r for r in rows if args["field"] in r.missing]
        elif args.get("only_missing", True):
            selected = [r for r in rows if r.missing]

        return _ok(
            {
                "project": session.project_label,
                "summary": stats,
                "volume_source": session.volume_source,
                "data_freshness": session.cache_note(),
                "returned": min(limit, len(selected)),
                "total_matching": len(selected),
                "rows": [
                    {
                        "event": r.event,
                        "volume": r.volume,
                        "missing": r.missing,
                        "tags": r.tags,
                    }
                    for r in selected[:limit]
                ],
                "note": (
                    "Descriptions are omitted here - use inspect_event before drafting any. "
                    "volume is null where no Mixpanel endpoint reported a count for that "
                    "event; treat null as unknown, not as zero, and do not call such an "
                    "event dead or unused. volume_source says which window the numbers "
                    "cover."
                ),
            }
        )

    @tool(
        "inspect_event",
        "Everything known about one event: its Lexicon definition, its properties with their "
        "definitions, and real sample values for each. You MUST call this before drafting a "
        "description for an event.",
        {
            "type": "object",
            "properties": {
                "event": {"type": "string"},
                "max_properties": {"type": "integer", "description": "default 25"},
                "sample_limit": {
                    "type": "integer",
                    "description": "values per property, default 8",
                },
            },
            "required": ["event"],
        },
    )
    async def inspect_event(args: dict[str, Any]) -> dict[str, Any]:
        name = str(args["event"])
        max_props = int(args.get("max_properties") or 25)
        sample_limit = int(args.get("sample_limit") or 8)

        try:
            defn = session.cached_event_def(name)
        except RateLimited as exc:
            return _err(str(exc))
        if defn is None:
            return _err(f"No Lexicon definition found for event {name!r}.")

        try:
            prop_names = session.ws.properties(name)[:max_props]
        except Exception as exc:
            return _err(f"Could not list properties for {name!r}: {exc}")

        prop_defs = session.property_defs(prop_names)

        # One API call per property, so fetch them concurrently - serially this
        # is the slowest thing the agent does and it does it on every event.
        def fetch(p: str) -> tuple[str, list[str]]:
            try:
                return p, session.ws.property_values(p, event=name, limit=sample_limit)
            except Exception:
                return p, []

        samples: dict[str, list[str]] = {}
        if prop_names:

            def run() -> dict[str, list[str]]:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    return dict(pool.map(fetch, prop_names))

            samples = await asyncio.to_thread(run)

        properties = [
            {
                "property": p,
                "description": getattr(prop_defs.get(p), "description", None),
                "display_name": getattr(prop_defs.get(p), "display_name", None),
                "sensitive": getattr(prop_defs.get(p), "sensitive", None),
                "sample_values": samples.get(p, [])[:sample_limit],
            }
            for p in prop_names
        ]

        frm, to = policy.volume_window
        volumes = session.volumes(days=int(policy["volume_days"]), frm=frm, to=to)
        return _ok(
            {
                "event": name,
                "volume": volumes.get(name),
                "volume_source": session.volume_source,
                "definition": {
                    "description": defn.description,
                    "display_name": defn.display_name,
                    "tags": list(defn.tags or []),
                    "verified": defn.verified,
                    "hidden": defn.hidden,
                    "platforms": list(defn.platforms or []),
                },
                "properties": properties,
            }
        )

    @tool(
        "list_tags",
        "List the Lexicon tags that exist in this project.",
        {"type": "object", "properties": {}},
    )
    async def list_tags(args: dict[str, Any]) -> dict[str, Any]:
        return _ok({"tags": session.tags()})

    @tool(
        "export_lexicon",
        "Export the full Lexicon to a local JSON file. Read-only; useful as a baseline "
        "before a documentation pass.",
        {"type": "object", "properties": {}},
    )
    async def export_lexicon(args: dict[str, Any]) -> dict[str, Any]:
        try:
            return _ok({"export": proposals.export_lexicon(session)})
        except Exception as exc:
            return _err(f"Export failed: {exc}")

    # --------------------------------------------------------------- proposals

    @tool(
        "propose_metadata",
        "Draft descriptions, display names and tags for events and/or properties, and write "
        "them to a local proposals file for the user to apply in Lexicon. This does NOT change "
        "Mixpanel - mpgov is read-only. Batch everything you have drafted into one call so the "
        "user reviews a single diff. Only include events you have inspected with inspect_event.",
        {
            "type": "object",
            "properties": {
                "events": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "event": {"type": "string"},
                            "description": {"type": "string"},
                            "display_name": {"type": "string"},
                            "tags": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["event"],
                    },
                },
                "properties": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "property": {"type": "string"},
                            "resource_type": {"type": "string", "enum": ["Event", "User"]},
                            "description": {"type": "string"},
                            "display_name": {"type": "string"},
                            "example_value": {"type": "string"},
                            "sensitive": {"type": "boolean"},
                        },
                        "required": ["property"],
                    },
                },
                "note": {
                    "type": "string",
                    "description": "Anything the reader should know: caveats, open questions, "
                    "events you deliberately skipped.",
                },
            },
        },
    )
    async def propose_metadata(args: dict[str, Any]) -> dict[str, Any]:
        if not (args.get("events") or args.get("properties")):
            return _err("Nothing proposed - supply events and/or properties.")
        diffs = proposals.diff_for(session, args)
        if not diffs:
            return _err(
                "Nothing would change - Lexicon already holds these values. "
                "Pick different entities or different fields."
            )
        paths = proposals.save(session, args, diffs, note=args.get("note"))
        return _ok(
            {
                "applied_to_mixpanel": False,
                "entities": [d.label for d in diffs],
                "files": paths,
                "next_step": "Review the markdown file, then apply in Lexicon.",
            }
        )

    return [
        list_projects,
        select_project,
        scan_lexicon,
        inspect_event,
        list_tags,
        export_lexicon,
        propose_metadata,
    ]


def build_server(
    session: GovSession,
    policy: Policy,
    session_id: Callable[[], str | None],
):
    """Construct the in-process MCP server bound to this Mixpanel session."""
    tools = build_tools(session, policy, session_id)
    registered = {t.name for t in tools}
    leaked = registered & set(MUTATING_TOOLS)
    if leaked:  # a guard, not a formality - see the module docstring
        raise RuntimeError(f"mpgov is read-only but registered mutating tools: {sorted(leaked)}")
    return create_sdk_mcp_server(name=SERVER_NAME, version="0.2.0", tools=tools)
