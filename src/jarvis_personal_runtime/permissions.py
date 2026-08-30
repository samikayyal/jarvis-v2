"""Saved host-plus-prefix command permissions for the personal runtime."""

from __future__ import annotations

import hashlib
import os
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock

_LOCK = RLock()
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_RULE_BLOCK = re.compile(
    r"(?ms)^\[\[saved_permissions\.rules\]\][ \t]*\r?\n"
    r".*?(?=^\[|\Z)"
)
_SAVED_HEADER = re.compile(r"(?m)^\[saved_permissions\][ \t]*$")
_NEXT_SECTION = re.compile(r"(?m)^\[(?!saved_permissions(?:\.|\]))[^\r\n]+\][ \t]*$")


class PermissionStoreError(RuntimeError):
    """The saved-permission section could not be read or updated safely."""


def normalize_host(host: str) -> str:
    if not isinstance(host, str):
        raise TypeError("permission host must be a string")
    if _CONTROL.search(host):
        raise ValueError("permission host must be non-empty and control-free")
    normalized = host.strip().rstrip(".").lower()
    if not normalized:
        raise ValueError("permission host must be non-empty and control-free")
    return normalized


@dataclass(frozen=True, slots=True, init=False)
class PermissionRule:
    host: str
    prefix: str
    id: str

    def __init__(self, host: str, prefix: str, id: str | None = None) -> None:
        normalized_host = normalize_host(host)
        if not isinstance(prefix, str):
            raise TypeError("permission prefix must be a string")
        normalized_prefix = prefix.strip()
        if not normalized_prefix or _CONTROL.search(normalized_prefix):
            raise ValueError("permission prefix must be non-empty and control-free")
        digest = hashlib.sha256(
            f"{normalized_host}\0{normalized_prefix}".encode()
        ).hexdigest()[:16]
        if id is not None and id != digest:
            raise ValueError("permission ID does not match its host and prefix")
        object.__setattr__(self, "host", normalized_host)
        object.__setattr__(self, "prefix", normalized_prefix)
        object.__setattr__(self, "id", digest)

    def matches(self, host: str, command: str) -> bool:
        if not isinstance(command, str):
            return False
        if normalize_host(host) != self.host:
            return False
        candidate = command.casefold() if self.host == "windows" else command
        prefix = self.prefix.casefold() if self.host == "windows" else self.prefix
        return candidate == prefix or candidate.startswith(f"{prefix} ")


class TomlPermissionStore:
    """Atomically update only ``jarvis.toml``'s saved-permission rules."""

    def __init__(self, path: str | os.PathLike[str]) -> None:
        self.path = Path(path)

    def list_rules(self) -> tuple[PermissionRule, ...]:
        with _LOCK:
            _, rules = self._read()
            return rules

    def list_permissions(self) -> tuple[PermissionRule, ...]:
        return self.list_rules()

    def matches(self, host: str, command: str) -> PermissionRule | None:
        return next(
            (rule for rule in self.list_rules() if rule.matches(host, command)),
            None,
        )

    def add(
        self, rule_or_host: PermissionRule | str, prefix: str | None = None
    ) -> PermissionRule:
        rule = (
            rule_or_host
            if isinstance(rule_or_host, PermissionRule)
            else PermissionRule(rule_or_host, _required_prefix(prefix))
        )
        with _LOCK:
            text, rules = self._read()
            for existing in rules:
                if (existing.host, existing.prefix) == (rule.host, rule.prefix):
                    return existing
            self._write(text, (*rules, rule))
            return rule

    async def save_permission(self, host: str, prefix: str) -> None:
        self.add(host, prefix)

    def remove(self, selector: PermissionRule | str) -> bool:
        rule_id = selector.id if isinstance(selector, PermissionRule) else str(selector)
        with _LOCK:
            text, rules = self._read()
            remaining = tuple(rule for rule in rules if rule.id != rule_id)
            if len(remaining) == len(rules):
                return False
            self._write(text, remaining)
            return True

    def forget_permission(self, selector: PermissionRule | str) -> None:
        self.remove(selector)

    def _read(self) -> tuple[str, tuple[PermissionRule, ...]]:
        try:
            text = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return "", ()
        except (OSError, UnicodeError) as exc:
            raise PermissionStoreError("jarvis.toml could not be read") from exc
        try:
            payload = tomllib.loads(text)
            saved = payload.get("saved_permissions", {})
            if not isinstance(saved, dict):
                raise TypeError("saved_permissions must be a table")
            raw_rules = saved.get("rules", [])
            if not isinstance(raw_rules, list):
                raise TypeError("saved_permissions.rules must be an array of tables")
            rules = tuple(
                PermissionRule(item["host"], item["prefix"], item.get("id"))
                for item in raw_rules
                if isinstance(item, dict)
            )
            if len(rules) != len(raw_rules):
                raise TypeError("saved permission must be a table")
            if len({rule.id for rule in rules}) != len(rules):
                raise ValueError("saved permissions must not contain duplicates")
        except (KeyError, TypeError, ValueError, tomllib.TOMLDecodeError) as exc:
            raise PermissionStoreError("saved permissions are malformed") from exc
        return text, rules

    def _write(self, original: str, rules: tuple[PermissionRule, ...]) -> None:
        rendered = _render_updated_document(original, rules)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            existing_mode = self.path.stat().st_mode if self.path.exists() else None
            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temporary = Path(stream.name)
                stream.write(rendered)
                stream.flush()
                os.fsync(stream.fileno())
            if existing_mode is not None:
                os.chmod(temporary, existing_mode)
            os.replace(temporary, self.path)
            temporary = None
        except OSError as exc:
            if temporary is not None:
                try:
                    temporary.unlink()
                except OSError:
                    pass
            raise PermissionStoreError(
                "saved permissions could not be written"
            ) from exc


def _required_prefix(prefix: str | None) -> str:
    if prefix is None:
        raise TypeError("permission prefix is required")
    return prefix


def _toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _render_updated_document(original: str, rules: tuple[PermissionRule, ...]) -> str:
    text = _RULE_BLOCK.sub("", original)
    header = _SAVED_HEADER.search(text)
    if header is None:
        if text and not text.endswith("\n"):
            text += "\n"
        if text and not text.endswith("\n\n"):
            text += "\n"
        text += "[saved_permissions]\n"
        insert_at = len(text)
    else:
        following = text[header.end() :]
        next_section = _NEXT_SECTION.search(following)
        insert_at = (
            len(text) if next_section is None else header.end() + next_section.start()
        )

    blocks = "".join(
        "\n[[saved_permissions.rules]]\n"
        f"host = {_toml_string(rule.host)}\n"
        f"prefix = {_toml_string(rule.prefix)}\n"
        f"id = {_toml_string(rule.id)}\n"
        for rule in rules
    )
    before = text[:insert_at]
    after = text[insert_at:]
    if before and not before.endswith("\n"):
        before += "\n"
    result = before + blocks + after
    return result if result.endswith("\n") else result + "\n"


__all__ = [
    "PermissionRule",
    "PermissionStoreError",
    "TomlPermissionStore",
    "normalize_host",
]
