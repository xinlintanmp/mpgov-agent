# mpgov

An interactive terminal agent for Mixpanel **Lexicon hygiene**: finding events and
properties with no description, display name or tags, and drafting copy to fill those
gaps from evidence rather than guesswork.

**mpgov is read-only against Mixpanel.** It never changes Lexicon. It drafts metadata,
shows you a diff, and writes a proposals file you apply yourself.

Built on the [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview)
with [Mixpanel Headless](https://mixpanel.github.io/mixpanel-headless/) as the data
layer. You talk to it, it answers, the conversation keeps its context.

```
you > which of my high-traffic events have no description?
you > draft the top five, but skip anything ambiguous
you > what's still untagged after that?
```

## Install

```bash
uv venv --python 3.13 && uv pip install -e .
```

Then authenticate Mixpanel once:

```bash
mp login
```

Run it:

```bash
mpgov
```

`mpgov --resume` continues the previous conversation. `mpgov --project <id>` binds a
specific project; `--policy <path>` points at a different policy file.

> If `mpgov` starts but reports `No module named 'mpgov'`, the editable install
> produced no path hook. Re-run `uv pip install -e . --reinstall-package mpgov`.

## Read-only, enforced three ways

One mechanism failing silently is how a guarantee like this gets lost, so there are
three independent ones:

1. **No mutating tool exists.** Nothing in `tools.py` calls a Mixpanel write endpoint,
   and `build_server` refuses to start if a mutating tool is ever registered.
2. **The gate denies mutations by name.** `MUTATING_TOOLS` lists the Mixpanel write
   operations and `approval.py` refuses any call matching them — plus any tool it does
   not recognise — so reintroducing one does not quietly grant it.
3. **Built-ins are removed by allowlist, not denylist.** `tools=[]` plus
   `strict_mcp_config=True` leaves the agent exactly seven tools and one MCP server.

`tests/test_readonly.py` asserts all three, including a static scan for SDK write
calls and a **live probe** of the running agent's actual tool surface. Static config
is not proof: probing is how 21 unblocked built-ins (`ToolSearch`, `Skill`,
`Workflow`, `CronCreate`, `SendMessage`, …) were found after a denylist looked fine.

> **The one thing to get right if you fork this:** never put a mutating or
> review tool in `allowed_tools`. An `allowed_tools` entry — including a
> `mcp__mpgov__*` wildcard — auto-approves the call *before* `can_use_tool` runs,
> silently bypassing the gate entirely.

## The rule the drafting rests on

The agent is forbidden from drafting a description for an event it has not inspected.
`inspect_event` returns the event's real property names and sample values, and the
copy has to come from those. If the evidence is ambiguous the agent is told to say so
and ask — a confidently wrong description in Lexicon is worse than an empty one,
because it stops the next person from looking. Caveats go in the proposal file's
`note` so they reach whoever applies it.

## Review, then a file you apply

`propose_metadata` shows the diff against current Lexicon and waits:

```
╭──────────── proposed change - event: Sign Up ────────────╮
│ description                                              │
│   before       (empty)                                   │
│   after        Fires when a person creates a new accou…  │
╰──────────────────────────────────────────────────────────╯
Save these proposals? yes / no / edit >
```

- **`e`** opens the payload in `$EDITOR` so you fix a bad description instead of
  rejecting and re-prompting.
- **`n`** returns the rejection to the agent, which is told to ask what was wrong
  rather than redraft the same thing.
- A proposal that would change nothing is refused before a file is written.

On yes you get two files in the current directory: a **Markdown** file to read, and a
**JSON** file to script against. Both say `applied: false`. `/proposals` lists what
has been drafted for the project; the log is at `~/.mpgov/proposals.jsonl`.

## Ranking gaps: the volume window

Ranking by traffic is what makes the output actionable, and the two Mixpanel endpoints
disagree — `event_counts` takes an explicit date range but returns an empty series on
some projects, while `top_events` always works but only covers today.

So `policy.yaml` offers both:

```yaml
volume_days: 30                # trailing window (uses today's counts)
volume_from: "2025-09-01"      # explicit range - takes precedence when both set
volume_to: "2025-09-30"
```

**Set the explicit range when the project's data sits outside the recent past.**
Otherwise every event ranks as unknown volume and the ordering silently degrades to
alphabetical, which is useless. The scan panel always names the window it used, so the
numbers and their label cannot drift apart.

Anything no source covers is reported as **unknown, not zero** — a dormant event and
an unmeasured one must not look alike, and the agent is explicitly told not to call an
event dead on a null count.

## Caching, and why it is not optional

Mixpanel's discovery endpoints are rate limited hard — once the quota is gone, the
retry window is **an hour**. A full scan costs an `events()` call, a
`get_event_definitions` call per 100 events, and a volume call, so re-fetching all of
it on every launch is what burns the quota.

Reads are cached to `~/.mpgov/cache/<project>/` with a 6-hour TTL. Relaunching is
free, and an interrupted session resumes without spending quota.

- `--refresh` (or `/refresh` in-session) drops the cache and re-fetches.
- `--cache-ttl <hours>` changes the lifetime.
- When the quota **is** exhausted, mpgov serves stale cached data rather than failing,
  and labels it `stale - rate limited`. With no cache at all it reports the retry
  window in plain language instead of raising.

The Mixpanel SDK retries a rate-limited call three times at 60-second intervals before
giving up, so the first failure after exhausting quota takes about three minutes to
surface. It logs each attempt while it waits.

## Commands

| | |
|---|---|
| `/scan [n]` | gap table, ranked by volume |
| `/refresh` | drop cached reads and re-fetch |
| `/project [id]` | list or switch projects |
| `/tags` | list Lexicon tags |
| `/proposals` | proposal files drafted for this project |
| `/cost` | session spend |
| `/help`, `/exit` | |

Anything not starting with `/` goes to the agent.

## Policy

`policy.yaml` (cwd, or `~/.mpgov/policy.yaml`) defines what counts as documented, the
house style the agent writes in, which events to ignore, and the volume window. It is
injected into the system prompt, so editing it changes the agent's behaviour with no
code change.

## Scope

Lexicon hygiene only. The tool layer is shaped so the other governance reads —
`run_audit()` schema drift, duplicate detection, PII flagging — slot in as additional
read tools without restructuring.

## Tests

```bash
python -m tests.test_readonly      # the read-only guarantee: 3 mechanisms + live probe
python -m tests.test_logic         # gap analysis, volume windows, diffing, export
python -m tests.test_approval      # the review gate, scripted keystrokes
python -m tests.test_cache         # cache hits, TTL expiry, stale fallback, rate limits
python -m tests.test_agent         # real agent, stub Mixpanel: gate + grounding + sandbox
python -m tests.test_deny          # rejection handling
python -m tests.test_live_readonly # live project; writes nothing at all
```

`test_logic`, `test_approval` and `test_cache` need no credentials and make no API
calls. `test_readonly` spawns one cheap agent session for the live probe. `test_agent`
and `test_deny` run the real model against the stubbed Mixpanel and assert what
matters: the gate is reached, the grounding rule is obeyed, no Mixpanel mutation is
attempted, and the sandbox holds.
