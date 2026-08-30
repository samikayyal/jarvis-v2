"""Focused tests for the replacement runtime's message-ID cache."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta, timezone

import pytest

from jarvis_personal_runtime.dedup import CacheError, MessageIdCache

UTC_NOW = datetime(2026, 8, 30, 12, 0, tzinfo=UTC)


def test_first_claim_is_new_and_second_claim_is_duplicate(tmp_path) -> None:
    path = tmp_path / "message-ids.json"
    cache = MessageIdCache(path)

    assert cache.claim("message-001", UTC_NOW) is True
    assert cache.claim("message-001", UTC_NOW + timedelta(seconds=1)) is False

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "message-001": UTC_NOW.isoformat()
    }


def test_entry_expires_at_the_exact_retention_cutoff(tmp_path) -> None:
    path = tmp_path / "message-ids.json"
    cache = MessageIdCache(path, retention=timedelta(days=7))

    assert cache.claim("message-001", UTC_NOW) is True
    assert (
        cache.claim(
            "message-001", UTC_NOW + timedelta(days=7) - timedelta(microseconds=1)
        )
        is False
    )
    assert cache.claim("message-001", UTC_NOW + timedelta(days=7)) is True


def test_claim_survives_a_new_cache_instance(tmp_path) -> None:
    path = tmp_path / "message-ids.json"

    assert MessageIdCache(path).claim("message-001", UTC_NOW) is True
    restarted = MessageIdCache(path)

    assert restarted.claim("message-001", UTC_NOW + timedelta(hours=1)) is False


def test_malformed_cache_fails_closed(tmp_path) -> None:
    path = tmp_path / "message-ids.json"
    path.write_text("not-json", encoding="utf-8")

    with pytest.raises(CacheError):
        MessageIdCache(path)


@pytest.mark.parametrize(
    ("message_id", "now", "exception"),
    [
        ("", UTC_NOW, ValueError),
        ("   ", UTC_NOW, ValueError),
        (None, UTC_NOW, TypeError),
        ("message-001", datetime.fromisoformat("2026-08-30T12:00:00"), ValueError),
        (
            "message-001",
            datetime(2026, 8, 30, 14, 0, tzinfo=timezone(timedelta(hours=2))),
            ValueError,
        ),
        ("message-001", "2026-08-30T12:00:00+00:00", TypeError),
    ],
)
def test_claim_validates_message_id_and_utc_datetime(
    tmp_path, message_id, now, exception
) -> None:
    cache = MessageIdCache(tmp_path / "message-ids.json")

    with pytest.raises(exception):
        cache.claim(message_id, now)


def test_concurrent_claims_of_one_id_have_one_winner(tmp_path) -> None:
    path = tmp_path / "message-ids.json"
    caches = [MessageIdCache(path) for _ in range(16)]

    def claim(cache: MessageIdCache) -> bool:
        return cache.claim("message-concurrent", UTC_NOW)

    with ThreadPoolExecutor(max_workers=len(caches)) as executor:
        results = list(executor.map(claim, caches))

    assert sum(results) == 1
    assert MessageIdCache(path).claim("message-concurrent", UTC_NOW) is False
