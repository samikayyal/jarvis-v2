from __future__ import annotations

from pathlib import Path

import pytest

from jarvis_personal_runtime.permissions import (
    PermissionRule,
    PermissionStoreError,
    TomlPermissionStore,
    normalize_host,
)


def test_rule_normalizes_host_and_matches_only_a_command_boundary() -> None:
    rule = PermissionRule(" Ubuntu.EXAMPLE. ", "  git status  ")

    assert rule.host == "ubuntu.example"
    assert rule.prefix == "git status"
    assert rule.id
    assert rule.matches("UBUNTU.EXAMPLE", "git status")
    assert rule.matches("ubuntu.example.", "git status --short")
    assert not rule.matches("ubuntu.example", "git statusx")
    assert not rule.matches("other.example", "git status")
    assert normalize_host(" Ubuntu.EXAMPLE. ") == "ubuntu.example"


@pytest.mark.parametrize("prefix", ["", "   ", "git\nstatus", "git\x00status"])
def test_rule_rejects_empty_or_control_prefixes(prefix: str) -> None:
    with pytest.raises(ValueError):
        PermissionRule("ubuntu", prefix)


def test_store_reads_and_writes_only_saved_permissions_and_is_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "jarvis.toml"
    original = """title = "preserve me"\n\n[model]\nname = "gpt-5.6-luna"\n\n[saved_permissions]\ncomment = "keep this key"\n\n[[saved_permissions.rules]]\nhost = "Ubuntu"\nprefix = "git status"\n\n[other]\nvalue = 42\n"""
    path.write_text(original, encoding="utf-8")
    store = TomlPermissionStore(path)

    rules = store.list_rules()
    assert len(rules) == 1
    assert rules[0].host == "ubuntu"
    assert rules[0].prefix == "git status"
    assert store.matches("ubuntu", "git status --short") == rules[0]
    assert store.matches("ubuntu", "git statusx") is None

    added = store.add("Windows.EXAMPLE", "Get-ChildItem")
    after_first_add = path.read_text(encoding="utf-8")
    assert 'title = "preserve me"' in after_first_add
    assert '[model]\nname = "gpt-5.6-luna"' in after_first_add
    assert "[other]\nvalue = 42" in after_first_add
    assert 'comment = "keep this key"' in after_first_add
    assert after_first_add.count("[[saved_permissions.rules]]") == 2

    duplicate = store.add(" windows.example. ", " Get-ChildItem ")
    assert duplicate == added
    assert len(store.list_rules()) == 2
    assert path.read_text(encoding="utf-8") == after_first_add

    assert store.remove(added.id)
    assert not store.remove(added.id)
    assert len(store.list_rules()) == 1
    assert "Get-ChildItem" not in path.read_text(encoding="utf-8")


def test_store_generates_deterministic_ids_and_atomic_replacement(
    tmp_path: Path,
) -> None:
    path = tmp_path / "jarvis.toml"
    path.write_text("answer = 7\n", encoding="utf-8")
    store = TomlPermissionStore(path)

    first = store.add(PermissionRule("ubuntu", "ls"))
    path_stat = path.stat()
    second = TomlPermissionStore(path).add(PermissionRule("ubuntu", "ls"))

    assert first.id == second.id
    assert first.id == PermissionRule("UBUNTU.", "ls").id
    assert path.stat().st_ino == path_stat.st_ino
    assert "answer = 7" in path.read_text(encoding="utf-8")


def test_store_rejects_malformed_or_ambiguous_permission_toml_without_writing(
    tmp_path: Path,
) -> None:
    path = tmp_path / "jarvis.toml"
    path.write_text("[saved_permissions]\nrules = {bad = true}\n", encoding="utf-8")
    before = path.read_bytes()
    store = TomlPermissionStore(path)

    with pytest.raises(PermissionStoreError):
        store.list_rules()
    with pytest.raises(PermissionStoreError):
        store.add("ubuntu", "ls")
    assert path.read_bytes() == before


def test_store_creates_missing_file_and_preserves_permissions_after_remove(
    tmp_path: Path,
) -> None:
    path = tmp_path / "nested" / "jarvis.toml"
    store = TomlPermissionStore(path)

    assert store.list_rules() == ()
    rule = store.add("ubuntu", "pwd")
    assert path.exists()
    assert store.list_rules() == (rule,)
    assert store.remove(rule)
    assert store.list_rules() == ()
    assert "[[saved_permissions.rules]]" not in path.read_text(encoding="utf-8")


def test_control_host_is_rejected() -> None:
    with pytest.raises(ValueError):
        PermissionRule("ubuntu\n", "ls")


def test_stored_permission_id_must_match_the_canonical_rule(tmp_path: Path) -> None:
    path = tmp_path / "jarvis.toml"
    path.write_text(
        '[[saved_permissions.rules]]\nhost = "ubuntu"\nprefix = "ls"\nid = "forged"\n',
        encoding="utf-8",
    )

    with pytest.raises(PermissionStoreError):
        TomlPermissionStore(path).list_rules()
