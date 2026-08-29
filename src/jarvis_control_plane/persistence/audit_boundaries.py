"""Append-only audit boundary adapters."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import ClassVar

from ..models import AuditEvidence, AuditFilter
from ..ports import AuditWriteError
from .audit_read import _SQLiteAuditReadMixin
from .audit_write import _SQLiteAuditWriteMixin
from .state_row_helpers import (
    _export_audit_json,
    _ReadOnlyAuditRecords,
    _resolve_audit_filter,
)


class InMemoryAuditBoundary:
    """Append-only redacted audit fake with deterministic failure injection."""

    def __init__(
        self, *, fail: bool = False, fail_on_append: int | None = None
    ) -> None:
        self._records: list[AuditEvidence] = []
        self._evidence_ids: set[str] = set()
        self.fail = fail
        self.fail_on_append = fail_on_append

    def append(self, evidence: AuditEvidence) -> None:
        self.append_batch((evidence,))

    def writable(self) -> bool:
        return not self.fail

    def append_batch(self, evidence: Sequence[AuditEvidence]) -> None:
        records = tuple(evidence)
        if any(not isinstance(record, AuditEvidence) for record in records):
            raise TypeError("audit boundary accepts only AuditEvidence")
        identifiers = [record.evidence_id for record in records]
        if len(set(identifiers)) != len(identifiers) or any(
            identifier in self._evidence_ids for identifier in identifiers
        ):
            raise AuditWriteError("duplicate audit evidence identifier")
        if self.fail:
            raise AuditWriteError("controlled audit append failure")
        first_number = len(self._records) + 1
        last_number = first_number + len(records) - 1
        if (
            records
            and self.fail_on_append is not None
            and first_number <= self.fail_on_append <= last_number
        ):
            raise AuditWriteError("controlled audit append failure")
        self._records.extend(records)
        self._evidence_ids.update(identifiers)

    @property
    def records(self) -> _ReadOnlyAuditRecords:
        """Return a read-only snapshot retained for ticket01 compatibility."""

        return _ReadOnlyAuditRecords(self._records)

    def safe_view(
        self,
        query: AuditFilter | None = None,
        **filters: object,
    ) -> tuple[AuditEvidence, ...]:
        resolved = _resolve_audit_filter(query, filters)
        records = tuple(record for record in self._records if resolved.matches(record))
        return records[: resolved.limit] if resolved.limit is not None else records

    def inspect(
        self,
        query: AuditFilter | None = None,
        **filters: object,
    ) -> tuple[AuditEvidence, ...]:
        """Alias for the local administration safe inspection view."""

        return self.safe_view(query, **filters)

    def export_json(
        self,
        query: AuditFilter | None = None,
        **filters: object,
    ) -> str:
        return _export_audit_json(self.safe_view(query, **filters))

    def export(
        self,
        query: AuditFilter | None = None,
        **filters: object,
    ) -> str:
        """Export only the filtered redacted view as deterministic JSON."""

        return self.export_json(query, **filters)


class SQLiteAuditBoundary(_SQLiteAuditWriteMixin, _SQLiteAuditReadMixin):
    """Append-only SQLite audit adapter with a safe local read surface.

    The append-only contract is protected at two local layers: SQLite triggers
    reject row updates/deletes even from another connection, and the adapter's
    connection authorizer rejects direct mutation attempts made through a
    connection shared with ticket01's state seam.
    """

    _AUDIT_COLUMNS: ClassVar[dict[str, tuple[str, int, int]]] = {
        "evidence_id": ("TEXT", 0, 1),
        "kind": ("TEXT", 1, 0),
        "occurred_at": ("TEXT", 1, 0),
        "event_id": ("TEXT", 0, 0),
        "request_id": ("TEXT", 0, 0),
        "message_id": ("TEXT", 0, 0),
        "operation_type": ("TEXT", 0, 0),
        "target_category": ("TEXT", 0, 0),
        "approval_decision": ("TEXT", 0, 0),
        "policy_decision": ("TEXT", 0, 0),
        "execution_status": ("TEXT", 0, 0),
        "outcome": ("TEXT", 1, 0),
        "actor": ("TEXT", 1, 0),
        "details_json": ("TEXT", 1, 0),
        "redacted": ("INTEGER", 1, 0),
    }
    _LEGACY_AUDIT_COLUMNS: ClassVar[dict[str, tuple[str, int, int]]] = {
        "evidence_id": ("TEXT", 0, 1),
        "kind": ("TEXT", 1, 0),
        "occurred_at": ("TEXT", 1, 0),
        "event_id": ("TEXT", 0, 0),
        "request_id": ("TEXT", 0, 0),
        "outcome": ("TEXT", 1, 0),
        "actor": ("TEXT", 1, 0),
        "details_json": ("TEXT", 1, 0),
        "redacted": ("INTEGER", 1, 0),
    }
    _AUDIT_TABLE_SQL_VARIANTS: ClassVar[frozenset[str]] = frozenset(
        {
            (
                "create table audit_evidence ( evidence_id text primary key, "
                "kind text not null, occurred_at text not null, event_id text, "
                "request_id text, message_id text, operation_type text, "
                "target_category text, approval_decision text, "
                "policy_decision text, execution_status text, outcome text not null, "
                "actor text not null, details_json text not null, "
                "redacted integer not null check (redacted = 1) )"
            ),
            (
                "create table audit_evidence ( evidence_id text primary key, "
                "kind text not null, occurred_at text not null, event_id text, "
                "request_id text, outcome text not null, actor text not null, "
                "details_json text not null, redacted integer not null "
                "check (redacted = 1) )"
            ),
        }
    )
    _AUDIT_TRIGGERS: ClassVar[dict[str, str]] = {
        "audit_evidence_no_update": (
            "create trigger audit_evidence_no_update before update on audit_evidence "
            "begin select raise(abort, 'audit evidence is append-only'); end"
        ),
        "audit_evidence_no_delete": (
            "create trigger audit_evidence_no_delete before delete on audit_evidence "
            "begin select raise(abort, 'audit evidence is append-only'); end"
        ),
    }

    def __init__(self, database: str | Path | sqlite3.Connection = ":memory:") -> None:
        if isinstance(database, sqlite3.Connection) and database.in_transaction:
            raise AuditWriteError(
                "caller-owned SQLite connection has an uncommitted transaction"
            )
        self._owns_connection = not isinstance(database, sqlite3.Connection)
        self._connection = (
            database
            if isinstance(database, sqlite3.Connection)
            else sqlite3.connect(str(database), check_same_thread=False)
        )
        self._connection.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        self._append_transaction_active = False
        try:
            self._connection.execute("PRAGMA recursive_triggers = ON")
            self._ensure_canonical_audit_table()
            self._connection.executescript(
                """
                CREATE INDEX IF NOT EXISTS audit_evidence_occurred_at
                    ON audit_evidence(occurred_at, evidence_id);
                CREATE INDEX IF NOT EXISTS audit_evidence_request_id
                    ON audit_evidence(request_id, evidence_id);
                CREATE INDEX IF NOT EXISTS audit_evidence_operation_type
                    ON audit_evidence(operation_type, evidence_id);
                CREATE INDEX IF NOT EXISTS audit_evidence_target_category
                    ON audit_evidence(target_category, evidence_id);
                CREATE INDEX IF NOT EXISTS audit_evidence_approval_decision
                    ON audit_evidence(approval_decision, evidence_id);
                CREATE INDEX IF NOT EXISTS audit_evidence_policy_decision
                    ON audit_evidence(policy_decision, evidence_id);
                CREATE INDEX IF NOT EXISTS audit_evidence_execution_status
                    ON audit_evidence(execution_status, evidence_id);
                CREATE INDEX IF NOT EXISTS audit_evidence_outcome
                    ON audit_evidence(outcome, evidence_id);
                CREATE TRIGGER IF NOT EXISTS audit_evidence_no_update
                    BEFORE UPDATE ON audit_evidence
                    BEGIN
                        SELECT RAISE(ABORT, 'audit evidence is append-only');
                    END;
                CREATE TRIGGER IF NOT EXISTS audit_evidence_no_delete
                    BEFORE DELETE ON audit_evidence
                    BEGIN
                        SELECT RAISE(ABORT, 'audit evidence is append-only');
                    END;
                """
            )
            self._connection.commit()
            self._connection.set_authorizer(self._authorize_sql)
        except sqlite3.Error as exc:
            self._connection.rollback()
            raise AuditWriteError("could not initialize SQLite audit") from exc

    def _ensure_canonical_audit_table(self) -> None:
        table = self._connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            ("audit_evidence",),
        ).fetchone()
        if table is None:
            self._connection.execute(
                """
                CREATE TABLE audit_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    event_id TEXT,
                    request_id TEXT,
                    message_id TEXT,
                    operation_type TEXT,
                    target_category TEXT,
                    approval_decision TEXT,
                    policy_decision TEXT,
                    execution_status TEXT,
                    outcome TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    redacted INTEGER NOT NULL CHECK (redacted = 1)
                )
                """
            )
            return

        columns = self._connection.execute(
            "PRAGMA table_info(audit_evidence)"
        ).fetchall()
        actual_columns = {
            row["name"]: (row["type"].upper(), row["notnull"], row["pk"])
            for row in columns
        }
        if actual_columns == self._AUDIT_COLUMNS:
            return
        if actual_columns != self._LEGACY_AUDIT_COLUMNS:
            # Leave unknown schemas for _assert_schema_integrity(), which keeps
            # unexpected or tampered layouts fail-closed on reads/appends.
            return

        normalized_sql = " ".join((table["sql"] or "").casefold().split())
        required_fragments = (
            "create table audit_evidence",
            "evidence_id text primary key",
            "kind text not null",
            "occurred_at text not null",
            "outcome text not null",
            "actor text not null",
            "details_json text not null",
            "redacted integer not null check (redacted = 1)",
        )
        if "on conflict" in normalized_sql or any(
            fragment not in normalized_sql for fragment in required_fragments
        ):
            raise AuditWriteError("unsupported legacy SQLite audit schema")
        self._rebuild_legacy_audit_table()

    def _rebuild_legacy_audit_table(self) -> None:
        migration_table = "audit_evidence_ticket03_migration"
        try:
            existing = self._connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (migration_table,),
            ).fetchone()
            if existing is not None:
                raise AuditWriteError("SQLite audit migration table already exists")
            self._connection.execute("BEGIN IMMEDIATE")
            self._connection.execute(
                f"""
                CREATE TABLE {migration_table} (
                    evidence_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    event_id TEXT,
                    request_id TEXT,
                    message_id TEXT,
                    operation_type TEXT,
                    target_category TEXT,
                    approval_decision TEXT,
                    policy_decision TEXT,
                    execution_status TEXT,
                    outcome TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    redacted INTEGER NOT NULL CHECK (redacted = 1)
                )
                """
            )
            self._connection.execute(
                f"""
                INSERT INTO {migration_table}(
                    evidence_id, kind, occurred_at, event_id, request_id,
                    message_id, operation_type, target_category,
                    approval_decision, policy_decision, execution_status,
                    outcome, actor, details_json, redacted
                )
                SELECT evidence_id, kind, occurred_at, event_id, request_id,
                       NULL, NULL, NULL, NULL, NULL, NULL,
                       outcome, actor, details_json, redacted
                FROM audit_evidence
                ORDER BY rowid
                """
            )
            self._connection.execute("DROP TABLE audit_evidence")
            self._connection.execute(
                """
                CREATE TABLE audit_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    occurred_at TEXT NOT NULL,
                    event_id TEXT,
                    request_id TEXT,
                    message_id TEXT,
                    operation_type TEXT,
                    target_category TEXT,
                    approval_decision TEXT,
                    policy_decision TEXT,
                    execution_status TEXT,
                    outcome TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    details_json TEXT NOT NULL,
                    redacted INTEGER NOT NULL CHECK (redacted = 1)
                )
                """
            )
            self._connection.execute(
                f"""
                INSERT INTO audit_evidence(
                    evidence_id, kind, occurred_at, event_id, request_id,
                    message_id, operation_type, target_category,
                    approval_decision, policy_decision, execution_status,
                    outcome, actor, details_json, redacted
                )
                SELECT evidence_id, kind, occurred_at, event_id, request_id,
                       message_id, operation_type, target_category,
                       approval_decision, policy_decision, execution_status,
                       outcome, actor, details_json, redacted
                FROM {migration_table}
                ORDER BY rowid
                """
            )
            self._connection.execute(f"DROP TABLE {migration_table}")
            self._connection.commit()
        except AuditWriteError:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise
        except sqlite3.Error as exc:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise AuditWriteError("could not migrate legacy SQLite audit") from exc
