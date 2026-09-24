"""Load the user-editable governance policy."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

DEFAULTS: dict[str, Any] = {
    "required_event_fields": ["description", "display_name"],
    "required_property_fields": ["description"],
    "description_style": "One or two sentences, present tense, plain English.",
    "display_name_style": "Title Case, human-readable, under 40 characters.",
    "ignore_events": [r"^\$", r"^mp_"],
    "allowed_tags": [],
    "volume_days": 30,
    "volume_from": None,
    "volume_to": None,
}


def _search_paths(explicit: str | None) -> list[Path]:
    if explicit:
        return [Path(explicit).expanduser()]
    return [
        Path.cwd() / "policy.yaml",
        Path.home() / ".mpgov" / "policy.yaml",
    ]


@dataclass
class Policy:
    data: dict[str, Any] = field(default_factory=lambda: dict(DEFAULTS))
    source: str = "built-in defaults"

    @classmethod
    def load(cls, path: str | None = None) -> "Policy":
        path = path or os.environ.get("MPGOV_POLICY")
        for candidate in _search_paths(path):
            if candidate.is_file():
                loaded = yaml.safe_load(candidate.read_text()) or {}
                merged = {**DEFAULTS, **loaded}
                return cls(data=merged, source=str(candidate))
        return cls()

    def __getitem__(self, key: str) -> Any:
        return self.data.get(key, DEFAULTS.get(key))

    @property
    def ignore_re(self) -> list[re.Pattern[str]]:
        return [re.compile(p) for p in self["ignore_events"] or []]

    def is_ignored(self, event_name: str) -> bool:
        return any(p.search(event_name) for p in self.ignore_re)

    @property
    def volume_window(self) -> tuple[str | None, str | None]:
        """Explicit (from, to) dates, or (None, None) for a trailing window."""
        frm, to = self["volume_from"], self["volume_to"]
        return (str(frm), str(to)) if frm and to else (None, None)

    def as_prompt_block(self) -> str:
        """The policy, rendered for the agent's system prompt."""
        return (
            f"Required on every event: {', '.join(self['required_event_fields'])}\n"
            f"Required on every property: {', '.join(self['required_property_fields'])}\n\n"
            f"Description style:\n{self['description_style'].strip()}\n\n"
            f"Display name style:\n{self['display_name_style'].strip()}"
        )
