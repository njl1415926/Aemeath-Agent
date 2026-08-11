from __future__ import annotations

import sqlite3
from pathlib import Path

from app.db.errors import StorageMigrationRequiredError
from app.db.stream_turns import (
    StreamRequestRecord,
    StreamTurnResolveOrReserve,
    apply_stream_turn_state_update,
    parse_stream_turn_state_delta,
    parse_stream_turn_warnings,
    stream_turn_state_delta_json,
    stream_turn_warnings_json,
)
from app.quota.schemas import (
    StreamQuotaComplete,
    StreamQuotaFail,
    StreamQuotaProviderStarted,
    StreamQuotaRecoveryResult,
    StreamQuotaReservationRecord,
    StreamQuotaResolveOrReserve,
    StreamQuotaResolveOutcome,
    StreamQuotaTerminalRecord,
    UsageDailySummary,
    UsageQueryRequest,
)
from app.schemas import MessageRecord, RelationshipState
from app.session.errors import (
    SessionArchivedError,
    SessionEndedError,
    SessionNotFoundError,
    SessionOwnershipError,
)
from app.utils.ids import new_id, utc_now


class StreamQuotaSQLiteUow:
    """SQLite UoW for the Phase 1 stream+quota transaction contract."""

    def __init__(self, db_path: Path):
        self._db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        if connection.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            connection.close()
            raise StorageMigrationRequiredError(reason="foreign_keys_not_enabled")
        return connection

    def get_daily_usage(self, *, request: UsageQueryRequest) -> UsageDailySummary | None:
        with self._connect() as conn:
            row = self._daily_row(conn, user_id=request.user_id, quota_day=request.quota_day)
            return self._daily_from_row(row) if row is not None else None

    def resolve_or_reserve_with_quota(
        self,
        *,
        request: StreamQuotaResolveOrReserve,
    ) -> StreamQuotaResolveOutcome:
        stream_request = self._stream_request(request.stream_request)
        if (
            stream_request.user_id != request.quota_plan.user_id
            or stream_request.active_character_id != request.quota_plan.character_id
        ):
            raise ValueError("stream_quota_plan_scope_mismatch")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            session_row = self._require_owned_session(
                conn,
                session_id=stream_request.session_id,
                user_id=stream_request.user_id,
                active_character_id=stream_request.active_character_id,
            )
            if session_row["status"] == "archived":
                raise SessionArchivedError("stream_quota_session_archived")
            record_row = self._find_stream_by_scope(
                conn,
                user_id=stream_request.user_id,
                session_id=stream_request.session_id,
                client_message_id=stream_request.client_message_id,
            )
            if session_row["status"] == "ended":
                if (
                    record_row is not None
                    and self._stream_matches(record_row, stream_request)
                    and record_row["status"] in {"completed", "failed"}
                ):
                    outcome = self._replay_outcome(conn, record_row)
                else:
                    raise SessionEndedError("stream_quota_session_ended")
            elif record_row is None:
                outcome = self._reserve_new(conn, request=request, stream_request=stream_request)
            elif not self._stream_matches(record_row, stream_request):
                outcome = StreamQuotaResolveOutcome(
                    kind="conflict",
                    record=self._stream_record_from_row(record_row),
                    quota_reservation=self._reservation_by_stream_id(conn, record_row["id"]),
                )
            elif record_row["status"] == "in_progress":
                outcome = StreamQuotaResolveOutcome(
                    kind="in_progress",
                    record=self._stream_record_from_row(record_row),
                    quota_reservation=self._reservation_by_stream_id(conn, record_row["id"]),
                )
            else:
                outcome = self._replay_outcome(conn, record_row)
            conn.commit()
            return outcome
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def mark_provider_started_with_quota(
        self,
        *,
        request: StreamQuotaProviderStarted,
    ) -> StreamQuotaReservationRecord:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            record_row = self._require_stream_record(
                conn,
                record_id=request.stream_request_id,
                turn_id=request.turn_id,
                user_id=request.user_id,
                session_id=request.session_id,
            )
            reservation_row = self._require_reservation_by_stream_id(
                conn,
                stream_request_id=request.stream_request_id,
            )
            if record_row["status"] == "in_progress" and (
                record_row["owner_runtime_id"] != request.owner_runtime_id
                or reservation_row["owner_runtime_id"] != request.owner_runtime_id
            ):
                raise StorageMigrationRequiredError(reason="usage_quota_owner_mismatch")
            if record_row["status"] == "in_progress" and reservation_row["status"] != "reserved":
                raise StorageMigrationRequiredError(reason="usage_quota_terminal_mismatch")
            if record_row["status"] != "in_progress":
                result = self._reservation_from_row(reservation_row)
                conn.commit()
                return result
            if reservation_row["charging_phase"] != "reserved_pre_provider":
                result = self._reservation_from_row(reservation_row)
                conn.commit()
                return result
            if reservation_row["estimator_version"] != request.prompt_estimate.estimator_version:
                raise StorageMigrationRequiredError(reason="usage_quota_estimator_mismatch")

            daily_row = self._require_daily(
                conn,
                user_id=reservation_row["user_id"],
                quota_day=reservation_row["quota_day"],
            )
            adjusted_reserved_tokens = request.prompt_estimate.reserved_tokens
            next_reserved_tokens = (
                daily_row["reserved_tokens"]
                - reservation_row["reserved_tokens"]
                + adjusted_reserved_tokens
            )
            if next_reserved_tokens + daily_row["settled_tokens"] > daily_row["token_limit"]:
                self._mark_stream_failed(
                    conn,
                    record_id=record_row["id"],
                    error_code="USAGE_QUOTA_EXCEEDED",
                    retryable=True,
                    reason="preparation_failed",
                )
                self._release_reservation(
                    conn,
                    reservation_row=reservation_row,
                    terminal_error_code="USAGE_QUOTA_EXCEEDED",
                    terminal_reason="quota_rejected_after_prepare",
                )
            else:
                now = utc_now()
                conn.execute(
                    """
                    UPDATE usage_quota_daily
                    SET reserved_tokens = ?, updated_at = ?
                    WHERE user_id = ? AND quota_day = ?
                    """,
                    (
                        next_reserved_tokens,
                        now,
                        reservation_row["user_id"],
                        reservation_row["quota_day"],
                    ),
                )
                conn.execute(
                    """
                    UPDATE usage_quota_reservations
                    SET charging_phase = 'provider_started',
                        reserved_tokens = ?,
                        reserved_input_tokens = ?,
                        reserved_output_tokens = ?,
                        prompt_messages_utf8_bytes = ?,
                        prompt_input_tokens = ?,
                        updated_at = ?,
                        provider_started_at = ?
                    WHERE id = ? AND status = 'reserved'
                    """,
                    (
                        adjusted_reserved_tokens,
                        request.prompt_estimate.prompt_input_tokens,
                        request.prompt_estimate.reserved_output_tokens,
                        request.prompt_estimate.prompt_messages_utf8_bytes,
                        request.prompt_estimate.prompt_input_tokens,
                        now,
                        now,
                        reservation_row["id"],
                    ),
                )
            updated = self._require_reservation_by_stream_id(
                conn,
                stream_request_id=request.stream_request_id,
            )
            conn.commit()
            return self._reservation_from_row(updated)
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def complete_with_quota(
        self,
        *,
        request: StreamQuotaComplete,
    ) -> StreamQuotaTerminalRecord:
        if not request.assistant_response.strip():
            raise ValueError("stream_quota_assistant_response_blank")
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            record_row = self._require_stream_record(
                conn,
                record_id=request.stream_request_id,
                turn_id=request.turn_id,
                user_id=request.user_id,
                session_id=request.session_id,
            )
            reservation_row = self._reservation_row_by_stream_id(conn, record_row["id"])
            if (
                record_row["status"] == "in_progress"
                and record_row["owner_runtime_id"] != request.owner_runtime_id
            ):
                raise StorageMigrationRequiredError(reason="usage_quota_owner_mismatch")
            if (
                record_row["status"] != "in_progress"
            ):
                reservation = (
                    self._reservation_from_row(reservation_row)
                    if reservation_row is not None
                    else None
                )
                result = self._terminal_result(conn, record_row, reservation)
                conn.commit()
                return result
            if reservation_row is None:
                raise StorageMigrationRequiredError(reason="usage_quota_reservation_missing")
            if reservation_row["owner_runtime_id"] != request.owner_runtime_id:
                raise StorageMigrationRequiredError(reason="usage_quota_owner_mismatch")
            if reservation_row["status"] != "reserved":
                raise StorageMigrationRequiredError(reason="usage_quota_terminal_mismatch")
            if reservation_row["charging_phase"] != "provider_started":
                raise StorageMigrationRequiredError(reason="usage_quota_provider_not_started")
            session_row = self._require_owned_session(
                conn,
                session_id=request.session_id,
                user_id=request.user_id,
                active_character_id=record_row["active_character_id"],
            )
            if session_row["status"] != "active":
                self._mark_stream_failed(
                    conn,
                    record_id=record_row["id"],
                    error_code=(
                        "SESSION_ARCHIVED"
                        if session_row["status"] == "archived"
                        else "SESSION_ENDED"
                    ),
                    retryable=False,
                    reason=(
                        "session_archived"
                        if session_row["status"] == "archived"
                        else "session_ended"
                    ),
                )
                self._terminalize_for_current_phase(
                    conn,
                    reservation_row=reservation_row,
                    terminal_error_code=(
                        "SESSION_ARCHIVED"
                        if session_row["status"] == "archived"
                        else "SESSION_ENDED"
                    ),
                    terminal_reason=(
                        "session_archived"
                        if session_row["status"] == "archived"
                        else "session_ended"
                    ),
                )
            else:
                self._complete_active_stream(conn, record_row=record_row, request=request)
                self._settle_reservation(
                    conn,
                    reservation_row=reservation_row,
                    output_utf8_bytes=request.output_estimate.response_utf8_bytes,
                    output_tokens=request.output_estimate.output_tokens,
                    delta_count=request.output_estimate.delta_count,
                    terminal_error_code=None,
                    terminal_reason=None,
                )
            updated_stream = self._require_stream_record(
                conn,
                record_id=record_row["id"],
                turn_id=record_row["turn_id"],
                user_id=record_row["user_id"],
                session_id=record_row["session_id"],
            )
            updated_reservation = self._reservation_by_stream_id(conn, record_row["id"])
            result = self._terminal_result(conn, updated_stream, updated_reservation)
            conn.commit()
            return result
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def fail_with_quota(self, *, request: StreamQuotaFail) -> StreamQuotaTerminalRecord:
        conn = self._connect()
        try:
            conn.execute("BEGIN IMMEDIATE")
            record_row = self._require_stream_record(
                conn,
                record_id=request.stream_request_id,
                turn_id=request.turn_id,
                user_id=request.user_id,
                session_id=request.session_id,
            )
            reservation_row = self._reservation_row_by_stream_id(conn, record_row["id"])
            if (
                record_row["status"] == "in_progress"
                and record_row["owner_runtime_id"] != request.owner_runtime_id
            ):
                raise StorageMigrationRequiredError(reason="usage_quota_owner_mismatch")
            if (
                record_row["status"] == "in_progress"
            ):
                if reservation_row is None:
                    raise StorageMigrationRequiredError(reason="usage_quota_reservation_missing")
                if reservation_row["owner_runtime_id"] != request.owner_runtime_id:
                    raise StorageMigrationRequiredError(reason="usage_quota_owner_mismatch")
                if reservation_row["status"] != "reserved":
                    raise StorageMigrationRequiredError(reason="usage_quota_terminal_mismatch")
                self._mark_stream_failed(
                    conn,
                    record_id=record_row["id"],
                    error_code=request.terminal_error_code,
                    retryable=request.terminal_retryable,
                    reason=request.terminal_reason,
                )
                self._terminalize_for_current_phase(
                    conn,
                    reservation_row=reservation_row,
                    terminal_error_code=request.terminal_error_code,
                    terminal_reason=self._quota_terminal_reason_for_fail(
                        reservation_row=reservation_row,
                        request=request,
                    ),
                )
                record_row = self._require_stream_record(
                    conn,
                    record_id=record_row["id"],
                    turn_id=record_row["turn_id"],
                    user_id=record_row["user_id"],
                    session_id=record_row["session_id"],
                )
                reservation = self._reservation_by_stream_id(conn, record_row["id"])
                result = self._terminal_result(conn, record_row, reservation)
                conn.commit()
                return result
            reservation = (
                self._reservation_from_row(reservation_row)
                if reservation_row is not None
                else None
            )
            result = self._terminal_result(conn, record_row, reservation)
            conn.commit()
            return result
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def recover_orphaned_with_quota(
        self,
        *,
        owner_runtime_id: str,
        limit: int = 100,
    ) -> StreamQuotaRecoveryResult:
        if not isinstance(owner_runtime_id, str) or not owner_runtime_id:
            raise ValueError("quota_owner_runtime_id_invalid")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 500:
            raise ValueError("quota_recovery_limit_invalid")
        conn = self._connect()
        released = 0
        settled = 0
        try:
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                """
                SELECT q.*
                FROM usage_quota_reservations AS q
                INNER JOIN chat_request_idempotency AS c ON c.id = q.stream_request_id
                WHERE q.status = 'reserved'
                  AND q.owner_runtime_id != ?
                  AND (c.status != 'in_progress' OR c.owner_runtime_id != ?)
                ORDER BY q.updated_at, q.id
                LIMIT ?
                """,
                (owner_runtime_id, owner_runtime_id, limit),
            ).fetchall()
            for reservation_row in rows:
                record_row = self._require_stream_record_by_id(
                    conn,
                    record_id=reservation_row["stream_request_id"],
                )
                if record_row["status"] == "in_progress":
                    self._mark_stream_failed(
                        conn,
                        record_id=record_row["id"],
                        error_code="CHAT_STREAM_INTERRUPTED",
                        retryable=False,
                        reason="runtime_orphaned",
                    )
                    if reservation_row["charging_phase"] == "provider_started":
                        self._settle_reservation(
                            conn,
                            reservation_row=reservation_row,
                            output_utf8_bytes=reservation_row["output_utf8_bytes"],
                            output_tokens=reservation_row["output_tokens"],
                            delta_count=reservation_row["delta_count"],
                            terminal_error_code="CHAT_STREAM_INTERRUPTED",
                            terminal_reason="runtime_orphaned_after_provider",
                            force_reserved_total=True,
                        )
                        settled += 1
                    else:
                        self._release_reservation(
                            conn,
                            reservation_row=reservation_row,
                            terminal_error_code="CHAT_STREAM_INTERRUPTED",
                            terminal_reason="runtime_orphaned_pre_provider",
                        )
                        released += 1
                elif record_row["status"] == "completed":
                    self._settle_reservation(
                        conn,
                        reservation_row=reservation_row,
                        output_utf8_bytes=reservation_row["output_utf8_bytes"],
                        output_tokens=reservation_row["output_tokens"],
                        delta_count=reservation_row["delta_count"],
                        terminal_error_code=None,
                        terminal_reason=None,
                        force_reserved_total=True,
                    )
                    settled += 1
                elif reservation_row["charging_phase"] == "provider_started":
                    self._settle_reservation(
                        conn,
                        reservation_row=reservation_row,
                        output_utf8_bytes=reservation_row["output_utf8_bytes"],
                        output_tokens=reservation_row["output_tokens"],
                        delta_count=reservation_row["delta_count"],
                        terminal_error_code=record_row["terminal_error_code"],
                        terminal_reason="runtime_orphaned_after_provider",
                        force_reserved_total=True,
                    )
                    settled += 1
                else:
                    self._release_reservation(
                        conn,
                        reservation_row=reservation_row,
                        terminal_error_code=record_row["terminal_error_code"],
                        terminal_reason="runtime_orphaned_pre_provider",
                    )
                    released += 1
            conn.commit()
            return StreamQuotaRecoveryResult(
                recovered_count=len(rows),
                released_count=released,
                settled_count=settled,
            )
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            conn.close()

    def _reserve_new(
        self,
        conn: sqlite3.Connection,
        *,
        request: StreamQuotaResolveOrReserve,
        stream_request: StreamTurnResolveOrReserve,
    ) -> StreamQuotaResolveOutcome:
        daily_row = self._daily_row(
            conn,
            user_id=request.quota_plan.user_id,
            quota_day=request.quota_plan.quota_day,
        )
        if daily_row is not None:
            self._require_daily_matches_plan(daily_row, request=request)
            if self._plan_exceeds_daily(daily_row, request=request):
                return StreamQuotaResolveOutcome(
                    kind="quota_exceeded",
                    daily_summary=self._daily_from_row(daily_row),
                    retry_after_seconds=request.retry_after_seconds,
                )
        elif (
            request.quota_plan.reserved_turns > request.quota_plan.turn_limit
            or request.quota_plan.reserved_tokens > request.quota_plan.token_limit
        ):
            return StreamQuotaResolveOutcome(
                kind="quota_exceeded",
                retry_after_seconds=request.retry_after_seconds,
            )

        now = utc_now()
        if daily_row is None:
            conn.execute(
                """
                INSERT INTO usage_quota_daily (
                    user_id, quota_day, turn_limit, token_limit, estimator_version,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    request.quota_plan.user_id,
                    request.quota_plan.quota_day,
                    request.quota_plan.turn_limit,
                    request.quota_plan.token_limit,
                    request.quota_plan.estimator_version,
                    now,
                    now,
                ),
            )
        cursor = conn.execute(
            """
            INSERT INTO messages (
                id, session_id, user_id, role, content, created_at, emotion_label, importance_score
            ) VALUES (?, ?, ?, 'user', ?, ?, NULL, NULL)
            """,
            (
                stream_request.user_message_id,
                stream_request.session_id,
                stream_request.user_id,
                stream_request.user_message_content,
                now,
            ),
        )
        stream_request_id = new_id("stream_request")
        cursor = conn.execute(
            """
            INSERT INTO chat_request_idempotency (
                id, user_id, session_id, client_message_id, active_character_id,
                request_fingerprint_version, request_fingerprint, turn_id, user_message_id,
                assistant_message_id, status, owner_runtime_id, warnings_json, state_delta_json,
                terminal_error_code, terminal_retryable, terminal_reason, created_at, updated_at,
                completed_at, failed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 'in_progress', ?, '[]', '{}',
                      NULL, NULL, NULL, ?, ?, NULL, NULL)
            """,
            (
                stream_request_id,
                stream_request.user_id,
                stream_request.session_id,
                stream_request.client_message_id,
                stream_request.active_character_id,
                stream_request.request_fingerprint_version,
                stream_request.request_fingerprint,
                stream_request.turn_id,
                stream_request.user_message_id,
                stream_request.owner_runtime_id,
                now,
                now,
            ),
        )
        reservation_id = new_id("quota_reservation")
        conn.execute(
            """
            INSERT INTO usage_quota_reservations (
                id, user_id, quota_day, surface, idempotency_key, session_id, character_id,
                stream_request_id, turn_id, owner_runtime_id, status, charging_phase,
                reserved_turns, reserved_tokens, estimator_version,
                context_budget_max_input_utf8_ceiling, reserved_input_tokens,
                reserved_output_tokens, created_at, updated_at
            ) VALUES (?, ?, ?, 'stream_chat', ?, ?, ?, ?, ?, ?, 'reserved',
                      'reserved_pre_provider', ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reservation_id,
                request.quota_plan.user_id,
                request.quota_plan.quota_day,
                _quota_idempotency_key(
                    session_id=stream_request.session_id,
                    client_message_id=stream_request.client_message_id,
                ),
                stream_request.session_id,
                request.quota_plan.character_id,
                stream_request_id,
                stream_request.turn_id,
                stream_request.owner_runtime_id,
                request.quota_plan.reserved_turns,
                request.quota_plan.reserved_tokens,
                request.quota_plan.estimator_version,
                request.quota_plan.context_budget_max_input_utf8_ceiling,
                request.quota_plan.reserved_input_tokens,
                request.quota_plan.reserved_output_tokens,
                now,
                now,
            ),
        )
        cursor = conn.execute(
            """
            UPDATE usage_quota_daily
            SET reserved_turns = reserved_turns + ?,
                reserved_tokens = reserved_tokens + ?,
                updated_at = ?
            WHERE user_id = ? AND quota_day = ?
            """,
            (
                request.quota_plan.reserved_turns,
                request.quota_plan.reserved_tokens,
                now,
                request.quota_plan.user_id,
                request.quota_plan.quota_day,
            ),
        )
        if cursor.rowcount != 1:
            raise StorageMigrationRequiredError(reason="usage_quota_daily_update_failed")
        record_row = self._require_stream_record(
            conn,
            record_id=stream_request_id,
            turn_id=stream_request.turn_id,
            user_id=stream_request.user_id,
            session_id=stream_request.session_id,
        )
        reservation_row = self._require_reservation_by_stream_id(
            conn,
            stream_request_id=stream_request_id,
        )
        daily_row = self._require_daily(
            conn,
            user_id=request.quota_plan.user_id,
            quota_day=request.quota_plan.quota_day,
        )
        return StreamQuotaResolveOutcome(
            kind="reserved",
            record=self._stream_record_from_row(record_row),
            quota_reservation=self._reservation_from_row(reservation_row),
            daily_summary=self._daily_from_row(daily_row),
        )

    def _terminalize_for_current_phase(
        self,
        conn: sqlite3.Connection,
        *,
        reservation_row: sqlite3.Row,
        terminal_error_code: str,
        terminal_reason: str,
    ) -> None:
        if reservation_row["charging_phase"] == "provider_started":
            self._settle_reservation(
                conn,
                reservation_row=reservation_row,
                output_utf8_bytes=reservation_row["output_utf8_bytes"],
                output_tokens=reservation_row["output_tokens"],
                delta_count=reservation_row["delta_count"],
                terminal_error_code=terminal_error_code,
                terminal_reason=terminal_reason,
                force_reserved_total=True,
            )
        else:
            self._release_reservation(
                conn,
                reservation_row=reservation_row,
                terminal_error_code=terminal_error_code,
                terminal_reason=terminal_reason,
            )

    def _settle_reservation(
        self,
        conn: sqlite3.Connection,
        *,
        reservation_row: sqlite3.Row,
        output_utf8_bytes: int,
        output_tokens: int,
        delta_count: int,
        terminal_error_code: str | None,
        terminal_reason: str | None,
        force_reserved_total: bool = False,
    ) -> None:
        if reservation_row["status"] != "reserved":
            return
        now = utc_now()
        if force_reserved_total:
            settled_tokens = reservation_row["reserved_tokens"]
        else:
            settled_tokens = min(
                reservation_row["reserved_tokens"],
                reservation_row["prompt_input_tokens"] + output_tokens,
            )
        cursor = conn.execute(
            """
            UPDATE usage_quota_daily
            SET reserved_turns = reserved_turns - ?,
                reserved_tokens = reserved_tokens - ?,
                settled_turns = settled_turns + ?,
                settled_tokens = settled_tokens + ?,
                updated_at = ?
            WHERE user_id = ? AND quota_day = ?
              AND reserved_turns >= ? AND reserved_tokens >= ?
            """,
            (
                reservation_row["reserved_turns"],
                reservation_row["reserved_tokens"],
                reservation_row["reserved_turns"],
                settled_tokens,
                now,
                reservation_row["user_id"],
                reservation_row["quota_day"],
                reservation_row["reserved_turns"],
                reservation_row["reserved_tokens"],
            ),
        )
        if cursor.rowcount != 1:
            raise StorageMigrationRequiredError(reason="usage_quota_counter_underflow")
        cursor = conn.execute(
            """
            UPDATE usage_quota_reservations
            SET status = 'settled',
                charging_phase = 'terminalized',
                settled_turns = ?,
                settled_tokens = ?,
                output_utf8_bytes = ?,
                output_tokens = ?,
                delta_count = ?,
                terminal_error_code = ?,
                terminal_reason = ?,
                updated_at = ?,
                settled_at = ?,
                released_at = NULL
            WHERE id = ? AND status = 'reserved'
            """,
            (
                reservation_row["reserved_turns"],
                settled_tokens,
                output_utf8_bytes,
                output_tokens,
                delta_count,
                terminal_error_code,
                terminal_reason,
                now,
                now,
                reservation_row["id"],
            ),
        )

    def _release_reservation(
        self,
        conn: sqlite3.Connection,
        *,
        reservation_row: sqlite3.Row,
        terminal_error_code: str | None,
        terminal_reason: str,
    ) -> None:
        if reservation_row["status"] != "reserved":
            return
        now = utc_now()
        cursor = conn.execute(
            """
            UPDATE usage_quota_daily
            SET reserved_turns = reserved_turns - ?,
                reserved_tokens = reserved_tokens - ?,
                released_turns = released_turns + ?,
                released_tokens = released_tokens + ?,
                updated_at = ?
            WHERE user_id = ? AND quota_day = ?
              AND reserved_turns >= ? AND reserved_tokens >= ?
            """,
            (
                reservation_row["reserved_turns"],
                reservation_row["reserved_tokens"],
                reservation_row["reserved_turns"],
                reservation_row["reserved_tokens"],
                now,
                reservation_row["user_id"],
                reservation_row["quota_day"],
                reservation_row["reserved_turns"],
                reservation_row["reserved_tokens"],
            ),
        )
        if cursor.rowcount != 1:
            raise StorageMigrationRequiredError(reason="usage_quota_counter_underflow")
        conn.execute(
            """
            UPDATE usage_quota_reservations
            SET status = 'released',
                charging_phase = 'terminalized',
                settled_turns = 0,
                settled_tokens = 0,
                terminal_error_code = ?,
                terminal_reason = ?,
                updated_at = ?,
                settled_at = NULL,
                released_at = ?
            WHERE id = ? AND status = 'reserved'
            """,
            (terminal_error_code, terminal_reason, now, now, reservation_row["id"]),
        )
        if cursor.rowcount != 1:
            raise StorageMigrationRequiredError(reason="usage_quota_reservation_update_failed")

    def _complete_active_stream(
        self,
        conn: sqlite3.Connection,
        *,
        record_row: sqlite3.Row,
        request: StreamQuotaComplete,
    ) -> None:
        now = utc_now()
        assistant_message_id = new_id("message")
        conn.execute(
            """
            INSERT INTO messages (
                id, session_id, user_id, role, content, created_at, emotion_label, importance_score
            ) VALUES (?, ?, ?, 'assistant', ?, ?, NULL, NULL)
            """,
            (
                assistant_message_id,
                record_row["session_id"],
                record_row["user_id"],
                request.assistant_response,
                now,
            ),
        )
        relationship_row = conn.execute(
            """
            SELECT mood, user_emotion, trust, affection, familiarity,
                   current_scene, last_topic, updated_at
            FROM relationship_states
            WHERE user_id = ? AND character_id = ?
            LIMIT 1
            """,
            (record_row["user_id"], record_row["active_character_id"]),
        ).fetchone()
        current = self._relationship_from_row(relationship_row)
        updated = apply_stream_turn_state_update(
            current=current,
            update=request.state_update,
            updated_at=now,
        )
        if relationship_row is None:
            conn.execute(
                """
                INSERT INTO relationship_states (
                    id, user_id, character_id, mood, user_emotion, trust, affection,
                    familiarity, current_scene, last_topic, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    new_id("relation"),
                    record_row["user_id"],
                    record_row["active_character_id"],
                    updated.mood,
                    updated.user_emotion,
                    updated.trust,
                    updated.affection,
                    updated.familiarity,
                    updated.current_scene,
                    updated.last_topic,
                    updated.updated_at,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE relationship_states
                SET mood = ?, user_emotion = ?, trust = ?, affection = ?, familiarity = ?,
                    current_scene = ?, last_topic = ?, updated_at = ?
                WHERE user_id = ? AND character_id = ?
                """,
                (
                    updated.mood,
                    updated.user_emotion,
                    updated.trust,
                    updated.affection,
                    updated.familiarity,
                    updated.current_scene,
                    updated.last_topic,
                    updated.updated_at,
                    record_row["user_id"],
                    record_row["active_character_id"],
                ),
            )
        conn.execute(
            """
            UPDATE chat_request_idempotency
            SET assistant_message_id = ?, status = 'completed', warnings_json = ?,
                state_delta_json = ?, terminal_error_code = NULL, terminal_retryable = NULL,
                terminal_reason = NULL, updated_at = ?, completed_at = ?, failed_at = NULL
            WHERE id = ? AND status = 'in_progress'
            """,
            (
                assistant_message_id,
                stream_turn_warnings_json(request.warnings),
                stream_turn_state_delta_json(request.state_update),
                now,
                now,
                record_row["id"],
            ),
        )

    def _mark_stream_failed(
        self,
        conn: sqlite3.Connection,
        *,
        record_id: str,
        error_code: str,
        retryable: bool,
        reason: str,
    ) -> None:
        now = utc_now()
        conn.execute(
            """
            UPDATE chat_request_idempotency
            SET assistant_message_id = NULL, status = 'failed', warnings_json = '[]',
                state_delta_json = '{}', terminal_error_code = ?, terminal_retryable = ?,
                terminal_reason = ?, updated_at = ?, completed_at = NULL, failed_at = ?
            WHERE id = ? AND status = 'in_progress'
            """,
            (error_code, int(retryable), reason, now, now, record_id),
        )

    def _quota_terminal_reason_for_fail(
        self,
        *,
        reservation_row: sqlite3.Row,
        request: StreamQuotaFail,
    ) -> str:
        if request.quota_terminal_reason:
            return request.quota_terminal_reason
        if request.terminal_reason == "runtime_orphaned":
            if reservation_row["charging_phase"] == "provider_started":
                return "runtime_orphaned_after_provider"
            return "runtime_orphaned_pre_provider"
        return request.terminal_reason

    def _require_daily(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: str,
        quota_day: str,
    ) -> sqlite3.Row:
        row = self._daily_row(conn, user_id=user_id, quota_day=quota_day)
        if row is None:
            raise StorageMigrationRequiredError(reason="usage_quota_daily_missing")
        return row

    def _daily_row(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: str,
        quota_day: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM usage_quota_daily WHERE user_id = ? AND quota_day = ?",
            (user_id, quota_day),
        ).fetchone()

    def _require_daily_matches_plan(
        self,
        daily_row: sqlite3.Row,
        *,
        request: StreamQuotaResolveOrReserve,
    ) -> None:
        if (
            daily_row["turn_limit"] != request.quota_plan.turn_limit
            or daily_row["token_limit"] != request.quota_plan.token_limit
            or daily_row["estimator_version"] != request.quota_plan.estimator_version
        ):
            raise StorageMigrationRequiredError(reason="usage_quota_daily_policy_mismatch")

    def _plan_exceeds_daily(
        self,
        daily_row: sqlite3.Row,
        *,
        request: StreamQuotaResolveOrReserve,
    ) -> bool:
        return (
            daily_row["reserved_turns"]
            + daily_row["settled_turns"]
            + request.quota_plan.reserved_turns
            > daily_row["turn_limit"]
            or daily_row["reserved_tokens"]
            + daily_row["settled_tokens"]
            + request.quota_plan.reserved_tokens
            > daily_row["token_limit"]
        )

    def _require_stream_record(
        self,
        conn: sqlite3.Connection,
        *,
        record_id: str,
        turn_id: str,
        user_id: str,
        session_id: str,
    ) -> sqlite3.Row:
        row = conn.execute(
            """
            SELECT *
            FROM chat_request_idempotency
            WHERE id = ? AND turn_id = ? AND user_id = ? AND session_id = ?
            LIMIT 1
            """,
            (record_id, turn_id, user_id, session_id),
        ).fetchone()
        if row is None:
            raise StorageMigrationRequiredError(reason="streaming_chat_record_not_found")
        return row

    def _require_stream_record_by_id(
        self,
        conn: sqlite3.Connection,
        *,
        record_id: str,
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM chat_request_idempotency WHERE id = ? LIMIT 1",
            (record_id,),
        ).fetchone()
        if row is None:
            raise StorageMigrationRequiredError(reason="streaming_chat_record_not_found")
        return row

    def _find_stream_by_scope(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: str,
        session_id: str,
        client_message_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            """
            SELECT *
            FROM chat_request_idempotency
            WHERE user_id = ? AND session_id = ? AND client_message_id = ?
            LIMIT 1
            """,
            (user_id, session_id, client_message_id),
        ).fetchone()

    def _reservation_by_stream_id(
        self,
        conn: sqlite3.Connection,
        stream_request_id: str,
    ) -> StreamQuotaReservationRecord | None:
        row = self._reservation_row_by_stream_id(conn, stream_request_id)
        return self._reservation_from_row(row) if row is not None else None

    def _reservation_row_by_stream_id(
        self,
        conn: sqlite3.Connection,
        stream_request_id: str,
    ) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT * FROM usage_quota_reservations WHERE stream_request_id = ?",
            (stream_request_id,),
        ).fetchone()

    def _require_reservation_by_stream_id(
        self,
        conn: sqlite3.Connection,
        *,
        stream_request_id: str,
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM usage_quota_reservations WHERE stream_request_id = ?",
            (stream_request_id,),
        ).fetchone()
        if row is None:
            raise StorageMigrationRequiredError(reason="usage_quota_reservation_missing")
        return row

    def _require_owned_session(
        self,
        conn: sqlite3.Connection,
        *,
        session_id: str,
        user_id: str,
        active_character_id: str,
    ) -> sqlite3.Row:
        row = conn.execute(
            "SELECT id, user_id, character_id, status FROM sessions WHERE id = ? LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None:
            raise SessionNotFoundError("stream_quota_session_not_found")
        if row["user_id"] != user_id or row["character_id"] != active_character_id:
            raise SessionOwnershipError("stream_quota_session_ownership_invalid")
        return row

    def _terminal_result(
        self,
        conn: sqlite3.Connection,
        record_row: sqlite3.Row,
        reservation: StreamQuotaReservationRecord | None,
    ) -> StreamQuotaTerminalRecord:
        daily = (
            self._daily_from_row(
                self._require_daily(
                    conn,
                    user_id=reservation.user_id,
                    quota_day=reservation.quota_day,
                )
            )
            if reservation is not None
            else None
        )
        return StreamQuotaTerminalRecord(
            stream_record=self._stream_record_from_row(record_row),
            quota_reservation=reservation,
            daily_summary=daily,
        )

    def _replay_outcome(
        self,
        conn: sqlite3.Connection,
        record_row: sqlite3.Row,
    ) -> StreamQuotaResolveOutcome:
        record = self._stream_record_from_row(record_row)
        reservation = self._reservation_by_stream_id(conn, record_row["id"])
        if record.status == "failed":
            return StreamQuotaResolveOutcome(
                kind="replay_failed",
                record=record,
                quota_reservation=reservation,
            )
        if record.status != "completed" or record.assistant_message_id is None:
            raise StorageMigrationRequiredError(reason="streaming_chat_replay_record_invalid")
        assistant_row = conn.execute(
            "SELECT * FROM messages WHERE id = ? LIMIT 1",
            (record.assistant_message_id,),
        ).fetchone()
        if (
            assistant_row is None
            or assistant_row["session_id"] != record.session_id
            or assistant_row["user_id"] != record.user_id
            or assistant_row["role"] != "assistant"
        ):
            raise StorageMigrationRequiredError(reason="streaming_chat_replay_assistant_invalid")
        return StreamQuotaResolveOutcome(
            kind="replay_completed",
            record=record,
            quota_reservation=reservation,
            assistant_message=self._message_from_row(assistant_row),
        )

    def _stream_matches(
        self,
        record_row: sqlite3.Row,
        request: StreamTurnResolveOrReserve,
    ) -> bool:
        return (
            record_row["request_fingerprint_version"] == request.request_fingerprint_version
            and record_row["request_fingerprint"] == request.request_fingerprint
        )

    def _stream_request(self, value: object) -> StreamTurnResolveOrReserve:
        if not isinstance(value, StreamTurnResolveOrReserve):
            raise ValueError("stream_quota_stream_request_invalid")
        return value

    def _stream_record_from_row(self, row: sqlite3.Row) -> StreamRequestRecord:
        status = row["status"]
        if status not in {"in_progress", "completed", "failed"}:
            raise StorageMigrationRequiredError(reason="streaming_chat_record_status_invalid")
        try:
            warnings = parse_stream_turn_warnings(row["warnings_json"])
            state_delta = parse_stream_turn_state_delta(
                row["state_delta_json"],
                status=status,
            )
        except (TypeError, ValueError) as exc:
            raise StorageMigrationRequiredError(
                reason="streaming_chat_record_payload_invalid"
            ) from exc
        if row["terminal_retryable"] not in {None, 0, 1}:
            raise StorageMigrationRequiredError(reason="streaming_chat_record_terminal_invalid")
        return StreamRequestRecord(
            id=row["id"],
            user_id=row["user_id"],
            session_id=row["session_id"],
            client_message_id=row["client_message_id"],
            active_character_id=row["active_character_id"],
            request_fingerprint_version=row["request_fingerprint_version"],
            request_fingerprint=row["request_fingerprint"],
            turn_id=row["turn_id"],
            user_message_id=row["user_message_id"],
            assistant_message_id=row["assistant_message_id"],
            status=status,
            owner_runtime_id=row["owner_runtime_id"],
            warnings=warnings,
            state_delta=state_delta,
            terminal_error_code=row["terminal_error_code"],
            terminal_retryable=(
                bool(row["terminal_retryable"]) if row["terminal_retryable"] is not None else None
            ),
            terminal_reason=row["terminal_reason"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            completed_at=row["completed_at"],
            failed_at=row["failed_at"],
        )

    def _reservation_from_row(self, row: sqlite3.Row) -> StreamQuotaReservationRecord:
        return StreamQuotaReservationRecord(
            id=row["id"],
            user_id=row["user_id"],
            quota_day=row["quota_day"],
            surface=row["surface"],
            idempotency_key=row["idempotency_key"],
            session_id=row["session_id"],
            character_id=row["character_id"],
            stream_request_id=row["stream_request_id"],
            turn_id=row["turn_id"],
            owner_runtime_id=row["owner_runtime_id"],
            status=row["status"],
            charging_phase=row["charging_phase"],
            reserved_turns=row["reserved_turns"],
            reserved_tokens=row["reserved_tokens"],
            settled_turns=row["settled_turns"],
            settled_tokens=row["settled_tokens"],
            estimator_version=row["estimator_version"],
            context_budget_max_input_utf8_ceiling=row[
                "context_budget_max_input_utf8_ceiling"
            ],
            reserved_input_tokens=row["reserved_input_tokens"],
            reserved_output_tokens=row["reserved_output_tokens"],
            prompt_messages_utf8_bytes=row["prompt_messages_utf8_bytes"],
            prompt_input_tokens=row["prompt_input_tokens"],
            output_utf8_bytes=row["output_utf8_bytes"],
            output_tokens=row["output_tokens"],
            delta_count=row["delta_count"],
            terminal_error_code=row["terminal_error_code"],
            terminal_reason=row["terminal_reason"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            provider_started_at=row["provider_started_at"],
            settled_at=row["settled_at"],
            released_at=row["released_at"],
        )

    def _daily_from_row(self, row: sqlite3.Row) -> UsageDailySummary:
        return UsageDailySummary(
            user_id=row["user_id"],
            quota_day=row["quota_day"],
            turn_limit=row["turn_limit"],
            token_limit=row["token_limit"],
            estimator_version=row["estimator_version"],
            reserved_turns=row["reserved_turns"],
            settled_turns=row["settled_turns"],
            released_turns=row["released_turns"],
            reserved_tokens=row["reserved_tokens"],
            settled_tokens=row["settled_tokens"],
            released_tokens=row["released_tokens"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def _message_from_row(self, row: sqlite3.Row) -> MessageRecord:
        return MessageRecord(
            id=row["id"],
            session_id=row["session_id"],
            user_id=row["user_id"],
            role=row["role"],
            content=row["content"],
            created_at=row["created_at"],
            emotion_label=row["emotion_label"],
            importance_score=row["importance_score"],
        )

    def _relationship_from_row(self, row: sqlite3.Row | None) -> RelationshipState | None:
        if row is None:
            return None
        return RelationshipState(
            mood=row["mood"] or "calm",
            user_emotion=row["user_emotion"] or "neutral",
            trust=row["trust"] if row["trust"] is not None else 50,
            affection=row["affection"] if row["affection"] is not None else 50,
            familiarity=row["familiarity"] if row["familiarity"] is not None else 50,
            current_scene=row["current_scene"] or "default_chat",
            last_topic=row["last_topic"],
            updated_at=row["updated_at"],
        )


def _quota_idempotency_key(*, session_id: str, client_message_id: str) -> str:
    return (
        f"stream_chat:v1:"
        f"session:{len(session_id)}:{session_id}:"
        f"client:{len(client_message_id)}:{client_message_id}"
    )
