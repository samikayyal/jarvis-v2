"""Session-scoped disclosure of trusted instructions and prepared tool schemas."""

from __future__ import annotations

import inspect
import json
from dataclasses import dataclass
from importlib.resources import files

from .responses import PreparedTools
from .runtime import ApprovalRequired

_TOOL_GROUPS = {
    "obsidian": frozenset(
        {"read_vault", "list_vault_tree", "edit_vault", "delete_vault"}
    ),
    "google": frozenset(
        {
            "google_gmail_search",
            "google_gmail_read_thread",
            "google_gmail_read_message",
            "google_gmail_send",
            "google_gmail_reply",
            "google_drive_search",
            "google_drive_metadata",
            "google_drive_read_text",
            "google_drive_export_text",
            "google_calendar_search",
            "google_calendar_list",
            "google_calendar_read",
            "google_calendar_create",
            "google_calendar_update",
        }
    ),
    "terminal": frozenset({"run_terminal"}),
    "reminders": frozenset(
        {"create_reminder", "list_reminders", "edit_reminder", "cancel_reminder"}
    ),
    "web": frozenset({"read_web_pages"}),
}


@dataclass(frozen=True)
class _Skill:
    description: str
    body: str


def _read_skill(name: str) -> _Skill:
    # Only names in the fixed registry reach this packaged resource lookup.
    text = (
        files("jarvis_personal_runtime")
        .joinpath("skills", name, "SKILL.md")
        .read_text(encoding="utf-8")
    )
    header, separator, body = text.removeprefix("---\n").partition("\n---\n")
    metadata = dict(line.split(": ", 1) for line in header.splitlines())
    if (
        not text.startswith("---\n")
        or not separator
        or not body.strip()
        or set(metadata) != {"name", "description"}
        or metadata["name"] != name
        or not metadata["description"].strip()
    ):
        raise ValueError(f"invalid packaged skill: {name}")
    return _Skill(metadata["description"], body.strip())


class SkillTools:
    """Filter model visibility and dispatch without changing tool permissions."""

    def __init__(self, tools: PreparedTools) -> None:
        self._tools = tools
        names = {str(item["name"]) for item in tools.definitions}
        if "load_skill" in names:
            raise ValueError("load_skill is reserved by the skill registry")
        self._skills = {
            name: _read_skill(name)
            for name, members in _TOOL_GROUPS.items()
            if names & members
        }
        self._owners = {
            member: name
            for name in self._skills
            for member in _TOOL_GROUPS[name]
            if member in names
        }
        self._loaded: set[str] = set()
        self._loader: dict[str, object] = {
            "type": "function",
            "name": "load_skill",
            "description": "Load a skill's instructions and tools for this working session.",
            "strict": True,
            "parameters": {
                "type": "object",
                "properties": {"name": {"type": "string", "enum": list(self._skills)}},
                "required": ["name"],
                "additionalProperties": False,
            },
        }

    def start_session(self) -> None:
        self._loaded.clear()

    @property
    def definitions(self) -> tuple[dict[str, object], ...]:
        visible = tuple(
            definition
            for definition in self._tools.definitions
            if self._owners.get(str(definition["name"])) in self._loaded
            or str(definition["name"]) not in self._owners
        )
        return ((self._loader,) if self._skills else ()) + visible

    @property
    def instructions(self) -> str:
        if not self._skills:
            return ""
        catalog = "\n".join(
            f"- {name}: {skill.description}" for name, skill in self._skills.items()
        )
        bodies = "\n\n".join(
            f"## Loaded skill: {name}\n{skill.body}"
            for name, skill in self._skills.items()
            if name in self._loaded
        )
        return (
            "## Skills\n"
            "Call load_skill before using a domain's tools. Load multiple skills when "
            "needed. Loaded instructions and tools remain available until the working "
            "session ends. Loading a skill does not authorize an action. Only exposed "
            "tools are available; a skill may describe optional operations.\n"
            f"{catalog}" + (f"\n\n{bodies}" if bodies else "")
        )

    async def execute(
        self, name: str, arguments: dict[str, object]
    ) -> str | ApprovalRequired:
        if name == "load_skill":
            skill = arguments.get("name")
            if set(arguments) != {"name"} or not isinstance(skill, str):
                raise ValueError("load_skill requires exactly one string name")
            if skill not in self._skills:
                raise ValueError(f"unavailable skill: {skill}")
            self._loaded.add(skill)
            # Bodies live in instructions, not repeated in conversation history.
            return json.dumps({"loaded": skill})
        owner = self._owners.get(name)
        if owner is not None and owner not in self._loaded:
            raise ValueError(f"load the {owner} skill before calling {name}")
        return await self._tools.execute(name, arguments)

    async def resume(self, continuation: object, *, approved: bool) -> str:
        return await self._tools.resume(continuation, approved=approved)

    def request_started(self) -> None:
        callback = getattr(self._tools, "request_started", None)
        if callable(callback):
            callback()

    async def close_request(self) -> None:
        callback = getattr(self._tools, "close_request", None)
        if callable(callback):
            result = callback()
            if inspect.isawaitable(result):
                await result

    def finish_answer(self, text: str) -> str:
        callback = getattr(self._tools, "finish_answer", None)
        if not callable(callback):
            return text
        return callback(text)
