"""Configuration loading for the fresh personal assistant runtime.

The replacement runtime deliberately has a small configuration boundary.  The
``.env`` file is read as a source of credentials, ``jarvis.toml`` contains only
non-secret runtime settings, and ``SYSTEM.md`` is the editable system prompt.
Loading never mutates the process environment and no writer in this module can
write the credential file. Runtime-owned paths stay beneath the runtime root;
the configured vault may be an external absolute directory.
"""

from __future__ import annotations

import ast
import ipaddress
import math
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

DEFAULT_ALLOWED_MODELS = (
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
)
DEFAULT_ALLOWED_REASONING_EFFORTS = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

# Keep defaults in one immutable mapping so a loader and callers constructing
# a RuntimeConfig manually cannot accidentally drift apart.
DEFAULTS: Mapping[str, Any] = MappingProxyType(
    {
        "model": "gpt-5.6-luna",
        "allowed_models": DEFAULT_ALLOWED_MODELS,
        "reasoning_effort": "medium",
        "allowed_reasoning_efforts": DEFAULT_ALLOWED_REASONING_EFFORTS,
        "inactivity_minutes": 60,
        "max_context_tokens": 100_000,
        "request_timeout_seconds": 600,
        "max_tool_rounds": 8,
        "command_timeout_seconds": 300,
        "max_output_chars": 65_536,
        "ubuntu_working_directory": ".",
        "ubuntu_read_only_prefixes": (),
        "windows_ssh_host": None,
        "windows_ssh_user": None,
        "windows_ssh_identity_file": None,
        "windows_working_directory": None,
        "windows_read_only_prefixes": (),
        "message_cache_path": "data/message-cache.json",
        "message_cache_retention_days": 7,
        "trace_path": "data/runtime-trace.jsonl",
        "trace_max_bytes": 10 * 1024 * 1024,
        "listener_host": None,
        "listener_port": None,
        "system_prompt_path": "SYSTEM.md",
        "vault_path": None,
        "openwa_api_base_url": None,
        "openwa_internal_session_id": None,
        "openwa_named_session": None,
        "openwa_authorized_operator_number": None,
        "openwa_operator_chat_id": None,
    }
)

# Readable aliases are useful to downstream code, while DEFAULTS remains the
# single source of the actual values.
DEFAULT_MODEL = DEFAULTS["model"]
DEFAULT_REASONING_EFFORT = DEFAULTS["reasoning_effort"]
DEFAULT_INACTIVITY_MINUTES = DEFAULTS["inactivity_minutes"]
DEFAULT_MAX_CONTEXT_TOKENS = DEFAULTS["max_context_tokens"]
DEFAULT_REQUEST_TIMEOUT_SECONDS = DEFAULTS["request_timeout_seconds"]
DEFAULT_MAX_TOOL_ROUNDS = DEFAULTS["max_tool_rounds"]
DEFAULT_COMMAND_TIMEOUT_SECONDS = DEFAULTS["command_timeout_seconds"]
DEFAULT_MAX_OUTPUT_CHARS = DEFAULTS["max_output_chars"]
DEFAULT_MESSAGE_CACHE_RETENTION_DAYS = DEFAULTS["message_cache_retention_days"]


class ConfigError(ValueError):
    """A configuration error tied to the exact file that caused it."""

    def __init__(self, path: str | Path, message: str) -> None:
        self.path = Path(path)
        self.message = message
        super().__init__(f"{self.path}: {message}")


@dataclass(frozen=True, slots=True)
class RuntimeSecrets:
    """Credentials loaded from ``.env``.

    The runtime requires credentials for OpenAI and OpenWA, including the
    webhook signing secret used to authenticate inbound messages.
    ``repr`` is redacted so an accidental diagnostic cannot print secrets.
    """

    openai_api_key: str = field(repr=False)
    openwa_api_key: str = field(repr=False)
    openwa_webhook_signing_secret: str = field(repr=False)

    @property
    def webhook_signing_secret(self) -> str:
        """Compatibility spelling for the OpenWA signing secret."""

        return self.openwa_webhook_signing_secret

    @property
    def openwa_webhook_secret(self) -> str:
        return self.openwa_webhook_signing_secret

    def __repr__(self) -> str:
        present = tuple(
            name
            for name, value in (
                ("openai_api_key", self.openai_api_key),
                ("openwa_api_key", self.openwa_api_key),
                (
                    "openwa_webhook_signing_secret",
                    self.openwa_webhook_signing_secret,
                ),
            )
            if value
        )
        return f"RuntimeSecrets(present={present!r})"


@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Validated, non-secret settings for one runtime process."""

    root: Path = field(default_factory=Path.cwd)
    model: str = DEFAULTS["model"]
    allowed_models: tuple[str, ...] = DEFAULTS["allowed_models"]
    reasoning_effort: str = DEFAULTS["reasoning_effort"]
    allowed_reasoning_efforts: tuple[str, ...] = DEFAULTS["allowed_reasoning_efforts"]
    inactivity_minutes: int = DEFAULTS["inactivity_minutes"]
    max_context_tokens: int = DEFAULTS["max_context_tokens"]
    request_timeout_seconds: float = DEFAULTS["request_timeout_seconds"]
    max_tool_rounds: int = DEFAULTS["max_tool_rounds"]
    command_timeout_seconds: float = DEFAULTS["command_timeout_seconds"]
    max_output_chars: int = DEFAULTS["max_output_chars"]
    ubuntu_working_directory: Path = Path(DEFAULTS["ubuntu_working_directory"])
    ubuntu_read_only_prefixes: tuple[str, ...] = DEFAULTS["ubuntu_read_only_prefixes"]
    windows_ssh_host: str | None = DEFAULTS["windows_ssh_host"]
    windows_ssh_user: str | None = DEFAULTS["windows_ssh_user"]
    windows_ssh_identity_file: Path | None = DEFAULTS["windows_ssh_identity_file"]
    windows_working_directory: str | None = DEFAULTS["windows_working_directory"]
    windows_read_only_prefixes: tuple[str, ...] = DEFAULTS["windows_read_only_prefixes"]
    message_cache_path: Path = Path(DEFAULTS["message_cache_path"])
    message_cache_retention_days: int = DEFAULTS["message_cache_retention_days"]
    trace_path: Path = Path(DEFAULTS["trace_path"])
    trace_max_bytes: int = DEFAULTS["trace_max_bytes"]
    listener_host: str | None = DEFAULTS["listener_host"]
    listener_port: int | None = DEFAULTS["listener_port"]
    system_prompt_path: Path = Path(DEFAULTS["system_prompt_path"])
    vault_path: Path | None = DEFAULTS["vault_path"]
    openwa_api_base_url: str | None = DEFAULTS["openwa_api_base_url"]
    openwa_internal_session_id: str | None = DEFAULTS["openwa_internal_session_id"]
    openwa_named_session: str | None = DEFAULTS["openwa_named_session"]
    openwa_authorized_operator_number: str | None = DEFAULTS[
        "openwa_authorized_operator_number"
    ]
    openwa_operator_chat_id: str | None = DEFAULTS["openwa_operator_chat_id"]

    @property
    def reasoning(self) -> str:
        return self.reasoning_effort

    @property
    def session_inactivity_minutes(self) -> int:
        return self.inactivity_minutes

    @property
    def inactivity_timeout_minutes(self) -> int:
        return self.inactivity_minutes

    @property
    def context_token_limit(self) -> int:
        return self.max_context_tokens

    @property
    def request_timeout(self) -> float:
        return self.request_timeout_seconds

    @property
    def command_timeout(self) -> float:
        return self.command_timeout_seconds

    @property
    def output_limit(self) -> int:
        return self.max_output_chars


@dataclass(frozen=True, slots=True)
class LoadedRuntimeConfig:
    """The complete immutable configuration loaded from one runtime root."""

    config: RuntimeConfig
    secrets: RuntimeSecrets
    system_prompt: str

    @property
    def runtime(self) -> RuntimeConfig:
        return self.config

    @property
    def runtime_config(self) -> RuntimeConfig:
        return self.config

    @property
    def runtime_secrets(self) -> RuntimeSecrets:
        return self.secrets

    @property
    def root(self) -> Path:
        return self.config.root


_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RUNTIME_KEYS = {
    "model",
    "allowed_models",
    "reasoning",
    "reasoning_effort",
    "allowed_reasoning_efforts",
    "allowed_reasoning",
    "inactivity_minutes",
    "session_inactivity_minutes",
    "inactivity_timeout_minutes",
    "max_context_tokens",
    "context_token_limit",
    "request_timeout_seconds",
    "request_timeout",
    "max_tool_rounds",
    "command_timeout_seconds",
    "command_timeout",
    "max_output_chars",
    "output_limit",
    "ubuntu_working_directory",
    "ubuntu_read_only_prefixes",
    "windows_ssh_host",
    "windows_ssh_user",
    "windows_ssh_identity_file",
    "windows_working_directory",
    "windows_read_only_prefixes",
    "message_cache_path",
    "message_cache_retention_days",
    "trace_path",
    "trace_max_bytes",
    "listener_host",
    "listener_port",
    "system_prompt_path",
    "vault_path",
    "openwa_api_base_url",
    "openwa_internal_session_id",
    "openwa_named_session",
    "openwa_authorized_operator_number",
    "openwa_operator_chat_id",
}


def _read_utf8(path: Path, *, kind: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise ConfigError(path, f"required {kind} file is missing") from exc
    except IsADirectoryError as exc:
        raise ConfigError(path, f"expected a {kind} file, found a directory") from exc
    except UnicodeDecodeError as exc:
        raise ConfigError(path, f"{kind} is not valid UTF-8") from exc
    except OSError as exc:
        raise ConfigError(path, f"cannot read {kind}: {exc}") from exc


def _parse_env(path: Path) -> dict[str, str]:
    text = _read_utf8(path, kind=".env")
    values: dict[str, str] = {}
    for line_number, original_line in enumerate(text.splitlines(), start=1):
        line = original_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        if "=" not in line:
            raise ConfigError(path, f"invalid .env entry on line {line_number}")
        name, raw_value = line.split("=", 1)
        name = name.strip()
        if not _ENV_NAME.fullmatch(name):
            raise ConfigError(path, f"invalid .env variable name on line {line_number}")
        raw_value = raw_value.strip()
        if (
            len(raw_value) >= 2
            and raw_value[0] == raw_value[-1]
            and raw_value[0] in "'\""
        ):
            try:
                parsed = ast.literal_eval(raw_value)
            except (SyntaxError, ValueError) as exc:
                raise ConfigError(
                    path, f"invalid quoted value on line {line_number}"
                ) from exc
            if not isinstance(parsed, str):
                raise ConfigError(path, f"invalid quoted value on line {line_number}")
            raw_value = parsed
        values[name] = raw_value
    return values


def _env_value(values: Mapping[str, str], path: Path, *names: str) -> str | None:
    present = [(name, values[name]) for name in names if name in values]
    nonempty = [(name, value) for name, value in present if value.strip()]
    if len({value for _, value in nonempty}) > 1:
        joined = ", ".join(name for name, _ in nonempty)
        raise ConfigError(path, f"conflicting values for {joined}")
    if not nonempty:
        return None
    return nonempty[0][1]


def _load_secrets(path: Path) -> RuntimeSecrets:
    values = _parse_env(path)
    openai = _env_value(values, path, "OPENAI_API_KEY", "JARVIS_OPENAI_API_KEY")
    if openai is None:
        raise ConfigError(path, "missing non-empty OPENAI_API_KEY")
    openwa_api_key = _env_value(
        values, path, "OPENWA_API_KEY", "OPENWA_API_MASTER_KEY"
    )
    if openwa_api_key is None:
        raise ConfigError(path, "missing non-empty OPENWA_API_KEY")
    openwa_webhook_signing_secret = _env_value(
        values,
        path,
        "OPENWA_WEBHOOK_SIGNING_SECRET",
        "OPENWA_WEBHOOK_SECRET",
        "JARVIS_WEBHOOK_SIGNING_SECRET",
    )
    if openwa_webhook_signing_secret is None:
        raise ConfigError(
            path, "missing non-empty OPENWA_WEBHOOK_SIGNING_SECRET"
        )
    return RuntimeSecrets(
        openai_api_key=openai,
        openwa_api_key=openwa_api_key,
        openwa_webhook_signing_secret=openwa_webhook_signing_secret,
    )


def _load_toml(path: Path) -> dict[str, Any]:
    text = _read_utf8(path, kind="jarvis.toml")
    try:
        result = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(path, f"invalid TOML: {exc}") from exc
    if not isinstance(result, dict):  # pragma: no cover - tomllib's contract
        raise ConfigError(path, "top-level TOML value must be a table")
    return result


def _ensure_table(value: Any, path: Path, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(path, f"{name} must be a table")
    return value


def _collect_runtime_values(raw: Mapping[str, Any], path: Path) -> dict[str, Any]:
    runtime = raw.get("runtime", {})
    runtime_table = _ensure_table(runtime, path, "[runtime]")
    for key in runtime_table:
        if key not in _RUNTIME_KEYS:
            raise ConfigError(path, f"unknown runtime setting: {key}")

    values: dict[str, Any] = {}
    for key, value in raw.items():
        if key not in {"runtime", "saved_permissions"}:
            if key not in _RUNTIME_KEYS:
                raise ConfigError(path, f"unknown top-level setting: {key}")
            values[key] = value
    for key, value in runtime_table.items():
        if key in values and values[key] != value:
            raise ConfigError(path, f"setting {key!r} is defined twice")
        values[key] = value
    return values


def _setting(values: Mapping[str, Any], *names: str, default: Any) -> Any:
    present = [(name, values[name]) for name in names if name in values]
    if not present:
        return default
    first = present[0][1]
    if any(value != first for _, value in present[1:]):
        names_text = ", ".join(name for name, _ in present)
        raise ValueError(f"conflicting aliases: {names_text}")
    return first


def _string(value: Any, path: Path, name: str, *, nonempty: bool = True) -> str:
    if not isinstance(value, str) or (nonempty and not value.strip()):
        raise ConfigError(path, f"{name} must be a non-empty string")
    return value.strip() if nonempty else value


def _string_list(value: Any, path: Path, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ConfigError(path, f"{name} must be a non-empty array of strings")
    result = tuple(_string(item, path, name) for item in value)
    if len(set(result)) != len(result):
        raise ConfigError(path, f"{name} must not contain duplicates")
    return result


def _string_list_or_empty(value: Any, path: Path, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ConfigError(path, f"{name} must be an array of strings")
    if not value:
        return ()
    result = tuple(_string(item, path, name) for item in value)
    if len(set(result)) != len(result):
        raise ConfigError(path, f"{name} must not contain duplicates")
    if any(
        any(ord(character) < 32 or ord(character) == 127 for character in item)
        for item in result
    ):
        raise ConfigError(path, f"{name} must not contain control characters")
    return result


def _positive_int(value: Any, path: Path, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ConfigError(path, f"{name} must be a positive integer")
    return value


def _optional_string(value: Any, path: Path, name: str) -> str | None:
    if value is None:
        return None
    return _string(value, path, name)


def _positive_number(value: Any, path: Path, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(path, f"{name} must be a positive number")
    result = float(value)
    if not math.isfinite(result) or result <= 0:
        raise ConfigError(path, f"{name} must be a positive number")
    return result


def _rooted_path(value: Any, root: Path, path: Path, name: str) -> Path:
    value_text = _string(value, path, name)
    candidate = Path(value_text)
    if candidate.is_absolute():
        raise ConfigError(path, f"{name} must be relative to the runtime root")
    root_resolved = root.resolve()
    resolved = (root / candidate).resolve()
    try:
        relative = resolved.relative_to(root_resolved)
    except ValueError as exc:
        raise ConfigError(path, f"{name} must remain beneath the runtime root") from exc
    if not relative.parts:
        raise ConfigError(path, f"{name} must identify a path beneath the runtime root")
    return resolved


def _optional_configured_path(
    value: Any, root: Path, path: Path, name: str
) -> Path | None:
    if value is None:
        return None
    candidate = Path(_string(value, path, name)).expanduser()
    if not candidate.is_absolute():
        candidate = root / candidate
    try:
        return candidate.resolve()
    except OSError as exc:
        raise ConfigError(path, f"cannot resolve {name}: {exc}") from exc


def _build_config(raw: Mapping[str, Any], root: Path, path: Path) -> RuntimeConfig:
    values = _collect_runtime_values(raw, path)

    try:
        model = _setting(values, "model", default=DEFAULTS["model"])
        allowed_models_value = _setting(
            values, "allowed_models", default=list(DEFAULTS["allowed_models"])
        )
        reasoning = _setting(
            values,
            "reasoning_effort",
            "reasoning",
            default=DEFAULTS["reasoning_effort"],
        )
        allowed_reasoning_value = _setting(
            values,
            "allowed_reasoning_efforts",
            "allowed_reasoning",
            default=list(DEFAULTS["allowed_reasoning_efforts"]),
        )
        inactivity = _setting(
            values,
            "inactivity_minutes",
            "session_inactivity_minutes",
            "inactivity_timeout_minutes",
            default=DEFAULTS["inactivity_minutes"],
        )
        context_limit = _setting(
            values,
            "max_context_tokens",
            "context_token_limit",
            default=DEFAULTS["max_context_tokens"],
        )
        request_timeout = _setting(
            values,
            "request_timeout_seconds",
            "request_timeout",
            default=DEFAULTS["request_timeout_seconds"],
        )
        command_timeout = _setting(
            values,
            "command_timeout_seconds",
            "command_timeout",
            default=DEFAULTS["command_timeout_seconds"],
        )
        output_limit = _setting(
            values,
            "max_output_chars",
            "output_limit",
            default=DEFAULTS["max_output_chars"],
        )
        ubuntu_working_directory_value = _setting(
            values,
            "ubuntu_working_directory",
            default=DEFAULTS["ubuntu_working_directory"],
        )
        ubuntu_read_only_prefixes_value = _setting(
            values,
            "ubuntu_read_only_prefixes",
            default=list(DEFAULTS["ubuntu_read_only_prefixes"]),
        )
        windows_ssh_host_value = _setting(
            values, "windows_ssh_host", default=DEFAULTS["windows_ssh_host"]
        )
        windows_ssh_user_value = _setting(
            values, "windows_ssh_user", default=DEFAULTS["windows_ssh_user"]
        )
        windows_ssh_identity_file_value = _setting(
            values,
            "windows_ssh_identity_file",
            default=DEFAULTS["windows_ssh_identity_file"],
        )
        windows_working_directory_value = _setting(
            values,
            "windows_working_directory",
            default=DEFAULTS["windows_working_directory"],
        )
        windows_read_only_prefixes_value = _setting(
            values,
            "windows_read_only_prefixes",
            default=list(DEFAULTS["windows_read_only_prefixes"]),
        )
        max_tool_rounds = _setting(
            values, "max_tool_rounds", default=DEFAULTS["max_tool_rounds"]
        )
        cache_path_value = _setting(
            values, "message_cache_path", default=DEFAULTS["message_cache_path"]
        )
        retention = _setting(
            values,
            "message_cache_retention_days",
            default=DEFAULTS["message_cache_retention_days"],
        )
        trace_path_value = _setting(
            values, "trace_path", default=DEFAULTS["trace_path"]
        )
        trace_max_bytes = _setting(
            values, "trace_max_bytes", default=DEFAULTS["trace_max_bytes"]
        )
        listener_host_value = _setting(
            values, "listener_host", default=DEFAULTS["listener_host"]
        )
        listener_port_value = _setting(
            values, "listener_port", default=DEFAULTS["listener_port"]
        )
        prompt_path_value = _setting(
            values, "system_prompt_path", default=DEFAULTS["system_prompt_path"]
        )
        vault_path_value = _setting(
            values, "vault_path", default=DEFAULTS["vault_path"]
        )
        openwa_api_base_url = _setting(
            values, "openwa_api_base_url", default=DEFAULTS["openwa_api_base_url"]
        )
        openwa_internal_session_id = _setting(
            values,
            "openwa_internal_session_id",
            default=DEFAULTS["openwa_internal_session_id"],
        )
        openwa_named_session = _setting(
            values,
            "openwa_named_session",
            default=DEFAULTS["openwa_named_session"],
        )
        openwa_authorized_operator_number = _setting(
            values,
            "openwa_authorized_operator_number",
            default=DEFAULTS["openwa_authorized_operator_number"],
        )
        openwa_operator_chat_id = _setting(
            values,
            "openwa_operator_chat_id",
            default=DEFAULTS["openwa_operator_chat_id"],
        )

        model = _string(model, path, "model")
        allowed_models = _string_list(allowed_models_value, path, "allowed_models")
        if any(item not in DEFAULT_ALLOWED_MODELS for item in allowed_models):
            raise ConfigError(path, "allowed_models contains an unsupported model")
        if model not in allowed_models:
            raise ConfigError(path, "model must be included in allowed_models")

        reasoning = _string(reasoning, path, "reasoning_effort")
        allowed_reasoning = _string_list(
            allowed_reasoning_value, path, "allowed_reasoning_efforts"
        )
        if any(
            item not in DEFAULT_ALLOWED_REASONING_EFFORTS for item in allowed_reasoning
        ):
            raise ConfigError(
                path, "allowed_reasoning_efforts contains an unsupported value"
            )
        if reasoning not in allowed_reasoning:
            raise ConfigError(
                path, "reasoning_effort must be included in allowed_reasoning_efforts"
            )

        inactivity = _positive_int(inactivity, path, "inactivity_minutes")
        context_limit = _positive_int(context_limit, path, "max_context_tokens")
        request_timeout = _positive_number(
            request_timeout, path, "request_timeout_seconds"
        )
        command_timeout = _positive_number(
            command_timeout, path, "command_timeout_seconds"
        )
        max_tool_rounds = _positive_int(max_tool_rounds, path, "max_tool_rounds")
        output_limit = _positive_int(output_limit, path, "max_output_chars")
        retention = _positive_int(retention, path, "message_cache_retention_days")
        trace_max_bytes = _positive_int(trace_max_bytes, path, "trace_max_bytes")
        listener_host = _optional_string(listener_host_value, path, "listener_host")
        listener_port = listener_port_value
        if (listener_host is None) != (listener_port is None):
            raise ConfigError(
                path, "listener_host and listener_port must be configured together"
            )
        if listener_host is not None:
            try:
                listener_address = ipaddress.IPv4Address(listener_host)
            except ipaddress.AddressValueError as exc:
                raise ConfigError(
                    path, "listener_host must be a private IPv4 address"
                ) from exc
            private_networks = (
                ipaddress.IPv4Network("10.0.0.0/8"),
                ipaddress.IPv4Network("172.16.0.0/12"),
                ipaddress.IPv4Network("192.168.0.0/16"),
            )
            if not any(listener_address in network for network in private_networks):
                raise ConfigError(path, "listener_host must be a private IPv4 address")
            listener_port = _positive_int(listener_port, path, "listener_port")
            if listener_port > 65_535:
                raise ConfigError(path, "listener_port must not exceed 65535")
        if retention != DEFAULTS["message_cache_retention_days"]:
            raise ConfigError(path, "message_cache_retention_days is fixed at 7")
        cache_path = _rooted_path(cache_path_value, root, path, "message_cache_path")
        trace_path = _rooted_path(trace_path_value, root, path, "trace_path")
        prompt_path = _rooted_path(prompt_path_value, root, path, "system_prompt_path")
        ubuntu_working_directory = _optional_configured_path(
            ubuntu_working_directory_value,
            root,
            path,
            "ubuntu_working_directory",
        )
        assert ubuntu_working_directory is not None
        ubuntu_read_only_prefixes = _string_list_or_empty(
            ubuntu_read_only_prefixes_value, path, "ubuntu_read_only_prefixes"
        )
        windows_ssh_host = _optional_string(
            windows_ssh_host_value, path, "windows_ssh_host"
        )
        windows_ssh_user = _optional_string(
            windows_ssh_user_value, path, "windows_ssh_user"
        )
        windows_ssh_identity_file = _optional_configured_path(
            windows_ssh_identity_file_value,
            root,
            path,
            "windows_ssh_identity_file",
        )
        windows_working_directory = _optional_string(
            windows_working_directory_value, path, "windows_working_directory"
        )
        windows_read_only_prefixes = _string_list_or_empty(
            windows_read_only_prefixes_value, path, "windows_read_only_prefixes"
        )
        windows_connection = (
            windows_ssh_host,
            windows_ssh_user,
            windows_ssh_identity_file,
            windows_working_directory,
        )
        if any(value is not None for value in windows_connection) and any(
            value is None for value in windows_connection
        ):
            raise ConfigError(
                path,
                "Windows SSH host, user, identity file, and working directory must be configured together",
            )
        vault_path = _optional_configured_path(
            vault_path_value, root, path, "vault_path"
        )
        openwa_api_base_url = _optional_string(
            openwa_api_base_url, path, "openwa_api_base_url"
        )
        openwa_internal_session_id = _optional_string(
            openwa_internal_session_id, path, "openwa_internal_session_id"
        )
        openwa_named_session = _optional_string(
            openwa_named_session, path, "openwa_named_session"
        )
        openwa_authorized_operator_number = _optional_string(
            openwa_authorized_operator_number,
            path,
            "openwa_authorized_operator_number",
        )
        openwa_operator_chat_id = _optional_string(
            openwa_operator_chat_id, path, "openwa_operator_chat_id"
        )
    except ConfigError:
        raise
    except ValueError as exc:
        raise ConfigError(path, str(exc)) from exc

    return RuntimeConfig(
        root=root,
        model=model,
        allowed_models=allowed_models,
        reasoning_effort=reasoning,
        allowed_reasoning_efforts=allowed_reasoning,
        inactivity_minutes=inactivity,
        max_context_tokens=context_limit,
        request_timeout_seconds=request_timeout,
        max_tool_rounds=max_tool_rounds,
        command_timeout_seconds=command_timeout,
        max_output_chars=output_limit,
        ubuntu_working_directory=ubuntu_working_directory,
        ubuntu_read_only_prefixes=ubuntu_read_only_prefixes,
        windows_ssh_host=windows_ssh_host,
        windows_ssh_user=windows_ssh_user,
        windows_ssh_identity_file=windows_ssh_identity_file,
        windows_working_directory=windows_working_directory,
        windows_read_only_prefixes=windows_read_only_prefixes,
        message_cache_path=cache_path,
        message_cache_retention_days=retention,
        trace_path=trace_path,
        trace_max_bytes=trace_max_bytes,
        listener_host=listener_host,
        listener_port=listener_port,
        system_prompt_path=prompt_path,
        vault_path=vault_path,
        openwa_api_base_url=openwa_api_base_url,
        openwa_internal_session_id=openwa_internal_session_id,
        openwa_named_session=openwa_named_session,
        openwa_authorized_operator_number=openwa_authorized_operator_number,
        openwa_operator_chat_id=openwa_operator_chat_id,
    )


def load_runtime_config(root: str | Path = ".") -> LoadedRuntimeConfig:
    """Load and validate ``.env``, ``jarvis.toml`` and ``SYSTEM.md``.

    ``root`` is the directory containing those three files. Runtime-owned paths
    are resolved beneath it, while an external absolute ``vault_path`` is kept.
    """

    root_path = Path(root).expanduser()
    try:
        root_path = root_path.resolve()
    except OSError as exc:
        raise ConfigError(root_path, f"cannot resolve runtime root: {exc}") from exc
    if not root_path.is_dir():
        raise ConfigError(root_path, "runtime root must be a directory")

    secrets = _load_secrets(root_path / ".env")
    toml_path = root_path / "jarvis.toml"
    raw = _load_toml(toml_path)
    config = _build_config(raw, root_path, toml_path)
    prompt = _read_utf8(config.system_prompt_path, kind="SYSTEM.md")
    if not prompt.strip():
        raise ConfigError(config.system_prompt_path, "SYSTEM.md must be non-empty")
    return LoadedRuntimeConfig(config=config, secrets=secrets, system_prompt=prompt)


def load_config(root: str | Path = ".") -> LoadedRuntimeConfig:
    """Short alias for :func:`load_runtime_config`."""

    return load_runtime_config(root)


__all__ = [
    "DEFAULTS",
    "DEFAULT_ALLOWED_MODELS",
    "DEFAULT_ALLOWED_REASONING_EFFORTS",
    "ConfigError",
    "LoadedRuntimeConfig",
    "RuntimeConfig",
    "RuntimeSecrets",
    "load_config",
    "load_runtime_config",
]
