"""SQLite audit append operations."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence

from ..models import AuditEvidence, ensure_utc
from ..ports import AuditWriteError


class _SQLiteAuditWriteMixin:
    def append(self, evidence: AuditEvidence) -> None:
        self.append_batch((evidence,))

    def writable(self) -> bool:
        """Probe the audit write lock without changing retained evidence."""

        with self._lock:
            try:
                self._connection.execute("BEGIN IMMEDIATE")
                self._connection.rollback()
                return True
            except sqlite3.Error:
                if self._connection.in_transaction:
                    self._connection.rollback()
                return False

    def append_batch(self, evidence: Sequence[AuditEvidence]) -> None:
        with self._lock:
            self._append_batch_locked(evidence)

    def _append_batch_locked(self, evidence: Sequence[AuditEvidence]) -> None:
        records = tuple(evidence)
        if any(not isinstance(record, AuditEvidence) for record in records):
            raise TypeError("audit boundary accepts only AuditEvidence")
        if not records:
            return
        if not self._owns_connection and self._connection.in_transaction:
            raise AuditWriteError(
                "caller-owned SQLite connection has an uncommitted transaction"
            )
        transaction_started = False
        try:
            self._connection.execute("BEGIN IMMEDIATE")
            transaction_started = True
            self._append_batch_in_transaction(records)
            self._connection.commit()
        except AuditWriteError:
            if transaction_started:
                self._connection.rollback()
            raise
        except sqlite3.Error as exc:
            if transaction_started or self._owns_connection:
                self._connection.rollback()
            raise AuditWriteError("could not append SQLite audit evidence") from exc
        finally:
            self._append_transaction_active = False

    def _append_batch_in_transaction(
        self,
        evidence: Sequence[AuditEvidence],
    ) -> None:
        """Append into a caller-owned transaction without committing it."""

        records = tuple(evidence)
        if any(not isinstance(record, AuditEvidence) for record in records):
            raise TypeError("audit boundary accepts only AuditEvidence")
        if not records:
            return
        identifiers = [record.evidence_id for record in records]
        if len(set(identifiers)) != len(identifiers):
            raise AuditWriteError("duplicate audit evidence identifier")

        self._append_transaction_active = True
        try:
            self._assert_schema_integrity()
            placeholders = ",".join("?" for _ in identifiers)
            existing = self._connection.execute(
                f"SELECT evidence_id FROM audit_evidence WHERE evidence_id IN ({placeholders})",
                identifiers,
            ).fetchone()
            if existing is not None:
                raise AuditWriteError("duplicate audit evidence identifier")
            before = self._connection.execute(
                "SELECT COUNT(*) AS count FROM audit_evidence"
            ).fetchone()["count"]
            for record in records:
                cursor = self._connection.execute(
                    """
                    INSERT INTO audit_evidence(
                        evidence_id, kind, occurred_at, event_id, request_id,
                        message_id, operation_type, target_category, approval_decision,
                        policy_decision, execution_status,
                        outcome, actor, details_json, redacted
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                    """,
                    (
                        record.evidence_id,
                        record.kind,
                        ensure_utc(record.occurred_at).isoformat(),
                        record.event_id,
                        record.request_id,
                        record.message_id,
                        record.operation_type,
                        record.target_category,
                        record.approval_decision,
                        record.policy_decision,
                        record.execution_status,
                        record.outcome,
                        record.actor,
                        json.dumps(
                            dict(record.details),
                            ensure_ascii=False,
                            separators=(",", ":"),
                            sort_keys=True,
                        ),
                    ),
                )
                if cursor.rowcount != 1:
                    raise AuditWriteError(
                        "SQLite audit append did not retain exactly one evidence row"
                    )
            after = self._connection.execute(
                "SELECT COUNT(*) AS count FROM audit_evidence"
            ).fetchone()["count"]
            if after - before != len(records):
                raise AuditWriteError(
                    "SQLite audit batch did not retain exactly one row per evidence"
                )
        except AuditWriteError:
            raise
        except sqlite3.Error as exc:
            raise AuditWriteError("could not append SQLite audit evidence") from exc
        finally:
            self._append_transaction_active = False
