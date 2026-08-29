"""SQLite audit safe-read operations."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from ..models import AuditEvidence, AuditFilter
from ..ports import AuditWriteError
from .state_row_helpers import _export_audit_json, _resolve_audit_filter


class _SQLiteAuditReadMixin:
    @property
    def records(self) -> tuple[AuditEvidence, ...]:
        return self.safe_view()

    def safe_view(
        self,
        query: AuditFilter | None = None,
        **filters: object,
    ) -> tuple[AuditEvidence, ...]:
        with self._lock:
            return self._safe_view_locked(query, **filters)

    def _safe_view_locked(
        self,
        query: AuditFilter | None = None,
        **filters: object,
    ) -> tuple[AuditEvidence, ...]:
        resolved = _resolve_audit_filter(query, filters)
        self._assert_schema_integrity()
        try:
            rows = self._select_rows(resolved)
        except sqlite3.Error as exc:
            raise AuditWriteError("could not read SQLite audit evidence") from exc
        try:
            records = tuple(self._evidence_from_row(row) for row in rows)
            records = tuple(record for record in records if resolved.matches(record))
            return records[: resolved.limit] if resolved.limit is not None else records
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise AuditWriteError(
                "stored audit evidence is not safe to inspect"
            ) from exc

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

    def _assert_schema_integrity(self) -> None:
        try:
            if (
                not self._owns_connection
                and self._connection.in_transaction
                and not self._append_transaction_active
            ):
                raise AuditWriteError(
                    "caller-owned SQLite connection has an uncommitted transaction"
                )
            table = self._connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                ("audit_evidence",),
            ).fetchone()
            columns = self._connection.execute(
                "PRAGMA table_info(audit_evidence)"
            ).fetchall()
            rows = self._connection.execute(
                """
                SELECT name, sql FROM sqlite_master
                WHERE type = 'trigger' AND tbl_name = 'audit_evidence'
                """
            ).fetchall()
        except AuditWriteError:
            raise
        except sqlite3.Error as exc:
            raise AuditWriteError("could not inspect SQLite audit schema") from exc

        actual_columns = {
            row["name"]: (row["type"].upper(), row["notnull"], row["pk"])
            for row in columns
        }
        normalized_table_sql = " ".join(
            (table["sql"] if table is not None and table["sql"] else "")
            .casefold()
            .split()
        )
        if (
            table is None
            or actual_columns != self._AUDIT_COLUMNS
            or normalized_table_sql not in self._AUDIT_TABLE_SQL_VARIANTS
        ):
            raise AuditWriteError("SQLite audit schema integrity check failed")

        actual_triggers = {
            row["name"]: " ".join((row["sql"] or "").casefold().split()) for row in rows
        }
        if actual_triggers != self._AUDIT_TRIGGERS:
            raise AuditWriteError("SQLite audit schema integrity check failed")

    def _authorize_sql(
        self,
        action: int,
        first_argument: str | None,
        second_argument: str | None,
        _database_name: str | None,
        _trigger_name: str | None,
    ) -> int:
        audit_table = (
            first_argument == "audit_evidence" or second_argument == "audit_evidence"
        )
        audit_trigger = (first_argument or "").startswith("audit_evidence_") or (
            second_argument or ""
        ).startswith("audit_evidence_")
        if (
            audit_table
            and action == sqlite3.SQLITE_INSERT
            and not self._append_transaction_active
            or audit_table
            and action
            in (
                sqlite3.SQLITE_UPDATE,
                sqlite3.SQLITE_DELETE,
                sqlite3.SQLITE_DROP_TABLE,
                sqlite3.SQLITE_ALTER_TABLE,
            )
            or audit_trigger
            and action == sqlite3.SQLITE_DROP_TRIGGER
        ):
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK

    def _select_rows(self, query: AuditFilter) -> list[sqlite3.Row]:
        conditions: list[str] = []
        parameters: list[object] = []
        if query.start_at is not None:
            conditions.append("occurred_at >= ?")
            parameters.append(query.start_at.isoformat())
        if query.end_at is not None:
            conditions.append("occurred_at < ?")
            parameters.append(query.end_at.isoformat())
        if query.on_date is not None:
            conditions.append("substr(occurred_at, 1, 10) = ?")
            parameters.append(query.on_date.isoformat())
        for column, value in (
            ("request_id", query.request_id),
            ("operation_type", query.operation_type),
            ("target_category", query.target_category),
            ("approval_decision", query.approval_decision),
            ("policy_decision", query.policy_decision),
            ("execution_status", query.execution_status),
            ("outcome", query.outcome),
        ):
            if value is not None:
                conditions.append(f"{column} = ?")
                parameters.append(value)
        statement = "SELECT * FROM audit_evidence"
        if conditions:
            statement += " WHERE " + " AND ".join(conditions)
        statement += " ORDER BY rowid"
        if query.limit is not None:
            statement += " LIMIT ?"
            parameters.append(query.limit)
        return self._connection.execute(statement, parameters).fetchall()

    @staticmethod
    def _evidence_from_row(row: sqlite3.Row) -> AuditEvidence:
        return AuditEvidence(
            evidence_id=row["evidence_id"],
            kind=row["kind"],
            occurred_at=datetime.fromisoformat(row["occurred_at"]),
            event_id=row["event_id"],
            request_id=row["request_id"],
            outcome=row["outcome"],
            actor=row["actor"],
            details=json.loads(row["details_json"]),
            redacted=bool(row["redacted"]),
            message_id=row["message_id"],
            operation_type=row["operation_type"],
            target_category=row["target_category"],
            approval_decision=row["approval_decision"],
            policy_decision=row["policy_decision"],
            execution_status=row["execution_status"],
        )

    def close(self) -> None:
        with self._lock:
            if self._owns_connection:
                self._connection.close()
