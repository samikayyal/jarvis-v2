from __future__ import annotations

from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from jarvis_personal_runtime.config import (
    DEFAULTS,
    ConfigError,
    LoadedRuntimeConfig,
    RuntimeConfig,
    RuntimeSecrets,
    load_runtime_config,
)


def _write_runtime_files(
    root: Path,
    *,
    env: str = "OPENAI_API_KEY=sk-test\nOPENWA_API_KEY=openwa-test\n",
    toml: str = "",
    system: str = "You are Jarvis.\n",
) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / ".env").write_text(env, encoding="utf-8")
    (root / "jarvis.toml").write_text(toml, encoding="utf-8")
    (root / "SYSTEM.md").write_text(system, encoding="utf-8")


def test_defaults_are_centralized_and_loading_is_immutable(tmp_path: Path) -> None:
    _write_runtime_files(tmp_path)

    loaded = load_runtime_config(tmp_path)

    assert isinstance(loaded, LoadedRuntimeConfig)
    assert isinstance(loaded.config, RuntimeConfig)
    assert isinstance(loaded.secrets, RuntimeSecrets)
    assert loaded.config.model == DEFAULTS["model"] == "gpt-5.6-luna"
    assert loaded.config.allowed_models == (
        "gpt-5.6-sol",
        "gpt-5.6-terra",
        "gpt-5.6-luna",
    )
    assert loaded.config.reasoning_effort == "medium"
    assert loaded.config.inactivity_minutes == 60
    assert loaded.config.max_context_tokens == 100_000
    assert loaded.config.request_timeout_seconds == 600
    assert loaded.config.max_tool_rounds == 8
    assert loaded.config.command_timeout_seconds == 300
    assert loaded.config.ubuntu_working_directory == tmp_path
    assert loaded.config.ubuntu_read_only_prefixes == ()
    assert loaded.config.max_output_chars == 65_536
    assert loaded.config.message_cache_path == tmp_path / "data" / "message-cache.json"
    assert loaded.config.trace_path == tmp_path / "data" / "runtime-trace.jsonl"
    assert loaded.config.trace_max_bytes == 10 * 1024 * 1024
    assert loaded.config.vault_path is None
    assert loaded.config.message_cache_retention_days == 7
    assert loaded.system_prompt == "You are Jarvis.\n"
    assert loaded.secrets.openai_api_key == "sk-test"
    assert loaded.secrets.openwa_api_key == "openwa-test"

    with pytest.raises(FrozenInstanceError):
        loaded.config.model = "gpt-5.6-sol"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        loaded.secrets.openai_api_key = "changed"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        loaded.system_prompt = "changed"  # type: ignore[misc]


def test_toml_overrides_are_validated_and_paths_are_rooted(tmp_path: Path) -> None:
    _write_runtime_files(
        tmp_path,
        toml="""
[runtime]
model = "gpt-5.6-sol"
reasoning_effort = "high"
inactivity_minutes = 12
max_context_tokens = 1234
request_timeout_seconds = 45
max_tool_rounds = 3
command_timeout_seconds = 17
max_output_chars = 999
ubuntu_working_directory = "work"
ubuntu_read_only_prefixes = ["pwd", "git status"]
message_cache_path = "state/message-ids.json"
trace_path = "trace/events.jsonl"
trace_max_bytes = 2048
vault_path = "vault"
""",
    )

    loaded = load_runtime_config(tmp_path)

    assert loaded.config.model == "gpt-5.6-sol"
    assert loaded.config.reasoning_effort == "high"
    assert loaded.config.inactivity_minutes == 12
    assert loaded.config.max_context_tokens == 1234
    assert loaded.config.request_timeout_seconds == 45
    assert loaded.config.max_tool_rounds == 3
    assert loaded.config.command_timeout_seconds == 17
    assert loaded.config.max_output_chars == 999
    assert loaded.config.ubuntu_working_directory == tmp_path / "work"
    assert loaded.config.ubuntu_read_only_prefixes == ("pwd", "git status")
    assert loaded.config.message_cache_path == tmp_path / "state" / "message-ids.json"
    assert loaded.config.trace_path == tmp_path / "trace" / "events.jsonl"
    assert loaded.config.trace_max_bytes == 2048
    assert loaded.config.vault_path == tmp_path / "vault"


def test_openwa_handoff_identities_load_from_toml_not_the_secret_file(
    tmp_path: Path,
) -> None:
    _write_runtime_files(
        tmp_path,
        toml="""
[runtime]
openwa_api_base_url = "http://172.17.0.1:2785/api"
openwa_internal_session_id = "session-001"
openwa_named_session = "jarvis"
openwa_authorized_operator_number = "962790000000@c.us"
openwa_operator_chat_id = "962790000000@c.us"
""",
    )

    loaded = load_runtime_config(tmp_path)

    assert loaded.config.openwa_api_base_url == "http://172.17.0.1:2785/api"
    assert loaded.config.openwa_internal_session_id == "session-001"
    assert loaded.config.openwa_named_session == "jarvis"
    assert loaded.config.openwa_authorized_operator_number == "962790000000@c.us"
    assert loaded.config.openwa_operator_chat_id == "962790000000@c.us"
    assert loaded.secrets.openwa_api_key == "openwa-test"


def test_external_absolute_vault_path_is_preserved(tmp_path: Path) -> None:
    external_vault = tmp_path.parent / "external-vault"
    _write_runtime_files(
        tmp_path,
        toml=f'[runtime]\nvault_path = "{external_vault.as_posix()}"\n',
    )

    loaded = load_runtime_config(tmp_path)

    assert loaded.config.vault_path == external_vault.resolve()


@pytest.mark.parametrize(
    ("toml", "needle"),
    [
        ('[runtime]\nmodel = "not-allowed"\n', "model"),
        ('[runtime]\nreasoning_effort = "bogus"\n', "reasoning_effort"),
        ("[runtime]\ninactivity_minutes = 0\n", "inactivity_minutes"),
        ("[runtime]\nmax_context_tokens = -1\n", "max_context_tokens"),
        ("[runtime]\nrequest_timeout_seconds = 0\n", "request_timeout_seconds"),
        ("[runtime]\nmax_tool_rounds = 0\n", "max_tool_rounds"),
        ("[runtime]\ncommand_timeout_seconds = 0\n", "command_timeout_seconds"),
        ("[runtime]\nmax_output_chars = 0\n", "max_output_chars"),
        ('[runtime]\nubuntu_read_only_prefixes = [""]\n', "ubuntu_read_only_prefixes"),
        ('[runtime]\nmessage_cache_path = "/outside.json"\n', "message_cache_path"),
        ('[runtime]\ntrace_path = "../outside.jsonl"\n', "trace_path"),
        ("[runtime]\nvault_path = 3\n", "vault_path"),
        ("[runtime]\ntrace_max_bytes = 0\n", "trace_max_bytes"),
        (
            "[runtime]\nmessage_cache_retention_days = 8\n",
            "message_cache_retention_days",
        ),
        ("[runtime]\nunknown = true\n", "unknown"),
    ],
)
def test_invalid_toml_is_a_path_specific_config_error(
    tmp_path: Path, toml: str, needle: str
) -> None:
    _write_runtime_files(tmp_path, toml=toml)

    with pytest.raises(ConfigError) as caught:
        load_runtime_config(tmp_path)

    assert caught.value.path == tmp_path / "jarvis.toml"
    assert needle in str(caught.value)


def test_message_cache_path_cannot_escape_root(tmp_path: Path) -> None:
    _write_runtime_files(
        tmp_path,
        toml='[runtime]\nmessage_cache_path = "../outside/message-ids.json"\n',
    )

    with pytest.raises(ConfigError) as caught:
        load_runtime_config(tmp_path)

    assert caught.value.path == tmp_path / "jarvis.toml"
    assert "message_cache_path" in str(caught.value)


def test_system_prompt_must_be_nonempty_utf8(tmp_path: Path) -> None:
    _write_runtime_files(tmp_path, system=" \n\t")

    with pytest.raises(ConfigError) as caught:
        load_runtime_config(tmp_path)
    assert caught.value.path == tmp_path / "SYSTEM.md"

    _write_runtime_files(tmp_path)
    (tmp_path / "SYSTEM.md").write_bytes(b"\xff\xfe")
    with pytest.raises(ConfigError) as caught:
        load_runtime_config(tmp_path)
    assert caught.value.path == tmp_path / "SYSTEM.md"


def test_env_is_read_only_and_missing_secret_is_reported(tmp_path: Path) -> None:
    _write_runtime_files(tmp_path, env="OPENAI_API_KEY=sk-original\n")
    before = (tmp_path / ".env").read_bytes()

    loaded = load_runtime_config(tmp_path)

    assert loaded.secrets.openai_api_key == "sk-original"
    assert (tmp_path / ".env").read_bytes() == before

    _write_runtime_files(tmp_path, env="OPENWA_API_KEY=only-openwa\n")
    with pytest.raises(ConfigError) as caught:
        load_runtime_config(tmp_path)
    assert caught.value.path == tmp_path / ".env"
    assert "OPENAI_API_KEY" in str(caught.value)


def test_malformed_files_report_the_file_path(tmp_path: Path) -> None:
    _write_runtime_files(tmp_path, toml="[runtime\n")
    with pytest.raises(ConfigError) as caught:
        load_runtime_config(tmp_path)
    assert caught.value.path == tmp_path / "jarvis.toml"

    _write_runtime_files(tmp_path)
    (tmp_path / ".env").write_bytes(b"OPENAI_API_KEY=\xff")
    with pytest.raises(ConfigError) as caught:
        load_runtime_config(tmp_path)
    assert caught.value.path == tmp_path / ".env"
