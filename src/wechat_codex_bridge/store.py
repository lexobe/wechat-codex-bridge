from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .models import (
    ConfirmationState,
    DeliveryReceipt,
    OutboundRequest,
    PendingAction,
    utc_now,
)

SCHEMA = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS conversation_mappings (
    channel TEXT NOT NULL,
    conversation_key TEXT NOT NULL,
    codex_conversation_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel, conversation_key)
);

CREATE TABLE IF NOT EXISTS inbound_events (
    channel TEXT NOT NULL,
    message_id TEXT NOT NULL,
    conversation_key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('processing', 'processed')),
    codex_conversation_id TEXT,
    received_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel, message_id)
);

CREATE TABLE IF NOT EXISTS confirmations (
    confirmation_id TEXT PRIMARY KEY,
    action_kind TEXT NOT NULL,
    target_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (
        state IN ('pending', 'approved', 'executing', 'consumed', 'rejected')
    ),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS outbound_receipts (
    request_id TEXT PRIMARY KEY,
    channel TEXT NOT NULL,
    recipient_key TEXT NOT NULL,
    payload_hash TEXT NOT NULL,
    gateway_receipt_id TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS session_control_operations (
    channel TEXT NOT NULL,
    account_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    command TEXT NOT NULL CHECK (command IN ('new', 'create')),
    target_key TEXT NOT NULL,
    message_hash TEXT NOT NULL,
    provider TEXT,
    state TEXT NOT NULL CHECK (
        state IN (
            'in_progress',
            'directory_created',
            'session_created',
            'binding_committed',
            'completed',
            'failed',
            'unknown_after_provider_create'
        )
    ),
    session_ref TEXT,
    created_directories_json TEXT,
    provider_create_started_at TEXT,
    provider_create_returned_at TEXT,
    uncertainty_reason TEXT,
    manual_review_required INTEGER NOT NULL DEFAULT 0
        CHECK (manual_review_required IN (0, 1)),
    manual_resolution_json TEXT,
    result_json TEXT,
    error TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel, account_id, sender_id, message_id)
);

CREATE TABLE IF NOT EXISTS session_inbound_routes (
    channel TEXT NOT NULL,
    account_id TEXT NOT NULL,
    sender_id TEXT NOT NULL,
    message_id TEXT NOT NULL,
    message_hash TEXT NOT NULL,
    state TEXT NOT NULL CHECK (state IN ('processing', 'completed', 'unknown')),
    result_json TEXT,
    received_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (channel, account_id, sender_id, message_id)
);
"""


class Store:
    """小型 SQLite 持久化层；策略和决策由其他层负责。"""

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._anchor: sqlite3.Connection | None = None
        if self.path == ":memory:":
            self._anchor = sqlite3.connect(":memory:")
            self._anchor.row_factory = sqlite3.Row
            self._anchor.executescript(SCHEMA)
        else:
            with self.connect() as connection:
                connection.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if self._anchor is not None:
            try:
                yield self._anchor
                self._anchor.commit()
            except Exception:
                self._anchor.rollback()
                raise
            return
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def get_mapping(self, channel: str, conversation_key: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT codex_conversation_id FROM conversation_mappings
                WHERE channel = ? AND conversation_key = ?
                """,
                (channel, conversation_key),
            ).fetchone()
        return None if row is None else str(row["codex_conversation_id"])

    def put_mapping(
        self, channel: str, conversation_key: str, codex_conversation_id: str
    ) -> str:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO conversation_mappings
                    (channel, conversation_key, codex_conversation_id, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(channel, conversation_key) DO NOTHING
                """,
                (channel, conversation_key, codex_conversation_id, now, now),
            )
        return self.get_mapping(channel, conversation_key) or codex_conversation_id

    def begin_inbound(
        self, channel: str, message_id: str, conversation_key: str
    ) -> tuple[bool, str | None]:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO inbound_events
                    (channel, message_id, conversation_key, status, received_at, updated_at)
                VALUES (?, ?, ?, 'processing', ?, ?)
                ON CONFLICT(channel, message_id) DO NOTHING
                """,
                (channel, message_id, conversation_key, now, now),
            )
            if cursor.rowcount == 1:
                return True, None
            row = connection.execute(
                """
                SELECT status, codex_conversation_id FROM inbound_events
                WHERE channel = ? AND message_id = ?
                """,
                (channel, message_id),
            ).fetchone()
        if row["status"] == "processing":
            raise RuntimeError("入站消息正在处理中")
        return False, str(row["codex_conversation_id"])

    def finish_inbound(
        self, channel: str, message_id: str, codex_conversation_id: str
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE inbound_events
                SET status = 'processed', codex_conversation_id = ?, updated_at = ?
                WHERE channel = ? AND message_id = ?
                """,
                (codex_conversation_id, utc_now(), channel, message_id),
            )

    def abandon_inbound(self, channel: str, message_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                DELETE FROM inbound_events
                WHERE channel = ? AND message_id = ? AND status = 'processing'
                """,
                (channel, message_id),
            )

    def create_confirmation(
        self,
        confirmation_id: str,
        action_kind: str,
        target_key: str,
        payload_hash: str,
    ) -> PendingAction:
        now = utc_now()
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO confirmations
                    (confirmation_id, action_kind, target_key, payload_hash,
                     state, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'pending', ?, ?)
                """,
                (confirmation_id, action_kind, target_key, payload_hash, now, now),
            )
        return PendingAction(
            confirmation_id, action_kind, target_key, ConfirmationState.PENDING, now
        )

    def set_confirmation_state(
        self, confirmation_id: str, expected: str, new_state: str
    ) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE confirmations SET state = ?, updated_at = ?
                WHERE confirmation_id = ? AND state = ?
                """,
                (new_state, utc_now(), confirmation_id, expected),
            )
        return cursor.rowcount == 1

    def claim_confirmation(
        self, confirmation_id: str, request: OutboundRequest, payload_hash: str
    ) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE confirmations SET state = 'executing', updated_at = ?
                WHERE confirmation_id = ? AND state = 'approved'
                  AND action_kind = ? AND target_key = ? AND payload_hash = ?
                """,
                (
                    utc_now(),
                    confirmation_id,
                    f"send:{request.purpose.value}",
                    request.recipient_key,
                    payload_hash,
                ),
            )
        return cursor.rowcount == 1

    def get_receipt(self, request_id: str) -> DeliveryReceipt | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT request_id, channel, recipient_key, gateway_receipt_id,
                       status, created_at
                FROM outbound_receipts WHERE request_id = ?
                """,
                (request_id,),
            ).fetchone()
        return None if row is None else DeliveryReceipt(**dict(row))

    def receipt_payload_hash(self, request_id: str) -> str | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT payload_hash FROM outbound_receipts WHERE request_id = ?",
                (request_id,),
            ).fetchone()
        return None if row is None else str(row["payload_hash"])

    def save_receipt(self, receipt: DeliveryReceipt, payload_hash: str) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO outbound_receipts
                    (request_id, channel, recipient_key, payload_hash,
                     gateway_receipt_id, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    receipt.request_id,
                    receipt.channel,
                    receipt.recipient_key,
                    payload_hash,
                    receipt.gateway_receipt_id,
                    receipt.status,
                    receipt.created_at,
                ),
            )

    def begin_session_control(
        self,
        *,
        channel: str,
        account_id: str,
        sender_id: str,
        message_id: str,
        command: str,
        target_key: str,
        message_hash: str,
    ) -> tuple[bool, dict[str, object]]:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO session_control_operations
                    (channel, account_id, sender_id, message_id, command,
                     target_key, message_hash, state, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'in_progress', ?, ?)
                ON CONFLICT(channel, account_id, sender_id, message_id) DO NOTHING
                """,
                (
                    channel,
                    account_id,
                    sender_id,
                    message_id,
                    command,
                    target_key,
                    message_hash,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM session_control_operations
                WHERE channel = ? AND account_id = ? AND sender_id = ?
                  AND message_id = ?
                """,
                (channel, account_id, sender_id, message_id),
            ).fetchone()
        record = dict(row)
        if (
            record["command"] != command
            or record["target_key"] != target_key
            or record["message_hash"] != message_hash
        ):
            raise ValueError("message_id 已绑定到其他控制请求")
        return cursor.rowcount == 1, record

    def get_session_control(
        self,
        *,
        channel: str,
        account_id: str,
        sender_id: str,
        message_id: str,
    ) -> dict[str, object] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM session_control_operations
                WHERE channel = ? AND account_id = ? AND sender_id = ?
                  AND message_id = ?
                """,
                (channel, account_id, sender_id, message_id),
            ).fetchone()
        return None if row is None else dict(row)

    def update_session_control(
        self,
        *,
        channel: str,
        account_id: str,
        sender_id: str,
        message_id: str,
        expected_states: tuple[str, ...],
        new_state: str,
        provider: str | None = None,
        session_ref: str | None = None,
        result: dict[str, object] | None = None,
        error: str | None = None,
    ) -> None:
        placeholders = ",".join("?" for _ in expected_states)
        now = utc_now()
        result_json = (
            json.dumps(result, ensure_ascii=False, sort_keys=True)
            if result is not None
            else None
        )
        with self.connect() as connection:
            cursor = connection.execute(
                f"""
                UPDATE session_control_operations
                SET state = ?,
                    provider = COALESCE(?, provider),
                    session_ref = COALESCE(?, session_ref),
                    result_json = COALESCE(?, result_json),
                    error = ?,
                    uncertainty_reason = CASE
                        WHEN ? = 'unknown_after_provider_create' THEN ?
                        ELSE uncertainty_reason
                    END,
                    manual_review_required = CASE
                        WHEN ? = 'unknown_after_provider_create' THEN 1
                        ELSE manual_review_required
                    END,
                    provider_create_returned_at = CASE
                        WHEN ? = 'unknown_after_provider_create'
                        THEN COALESCE(provider_create_returned_at, ?)
                        ELSE provider_create_returned_at
                    END,
                    updated_at = ?
                WHERE channel = ? AND account_id = ? AND sender_id = ?
                  AND message_id = ? AND state IN ({placeholders})
                """,
                (
                    new_state,
                    provider,
                    session_ref,
                    result_json,
                    error,
                    new_state,
                    error,
                    new_state,
                    new_state,
                    now,
                    now,
                    channel,
                    account_id,
                    sender_id,
                    message_id,
                    *expected_states,
                ),
            )
        if cursor.rowcount != 1:
            raise RuntimeError(
                f"控制事务状态无法从 {expected_states} 更新为 {new_state}"
            )

    def mark_session_directories_created(
        self,
        *,
        channel: str,
        account_id: str,
        sender_id: str,
        message_id: str,
        directories: list[str],
    ) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE session_control_operations
                SET state = 'directory_created',
                    created_directories_json = ?,
                    updated_at = ?
                WHERE channel = ? AND account_id = ? AND sender_id = ?
                  AND message_id = ? AND state = 'in_progress'
                """,
                (
                    json.dumps(directories, ensure_ascii=False),
                    utc_now(),
                    channel,
                    account_id,
                    sender_id,
                    message_id,
                ),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("已创建目录状态无法持久化")

    def mark_provider_create_started(
        self,
        *,
        channel: str,
        account_id: str,
        sender_id: str,
        message_id: str,
        provider: str,
    ) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE session_control_operations
                SET provider = ?,
                    provider_create_started_at = ?,
                    updated_at = ?
                WHERE channel = ? AND account_id = ? AND sender_id = ?
                  AND message_id = ?
                  AND state IN ('in_progress', 'directory_created')
                  AND provider_create_started_at IS NULL
                """,
                (
                    provider,
                    utc_now(),
                    utc_now(),
                    channel,
                    account_id,
                    sender_id,
                    message_id,
                ),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("provider 创建开始状态无法持久化")

    def mark_session_created(
        self,
        *,
        channel: str,
        account_id: str,
        sender_id: str,
        message_id: str,
        provider: str,
        session_ref: str,
    ) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE session_control_operations
                SET state = 'session_created',
                    provider = ?,
                    session_ref = ?,
                    provider_create_returned_at = ?,
                    updated_at = ?
                WHERE channel = ? AND account_id = ? AND sender_id = ?
                  AND message_id = ?
                  AND state IN ('in_progress', 'directory_created')
                  AND provider_create_started_at IS NOT NULL
                """,
                (
                    provider,
                    session_ref,
                    utc_now(),
                    utc_now(),
                    channel,
                    account_id,
                    sender_id,
                    message_id,
                ),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("SESSION_CREATED 无法持久化")

    def begin_session_route(
        self,
        *,
        channel: str,
        account_id: str,
        sender_id: str,
        message_id: str,
        message_hash: str,
    ) -> tuple[bool, dict[str, object]]:
        now = utc_now()
        with self.connect() as connection:
            cursor = connection.execute(
                """
                INSERT INTO session_inbound_routes
                    (channel, account_id, sender_id, message_id, message_hash,
                     state, received_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'processing', ?, ?)
                ON CONFLICT(channel, account_id, sender_id, message_id) DO NOTHING
                """,
                (
                    channel,
                    account_id,
                    sender_id,
                    message_id,
                    message_hash,
                    now,
                    now,
                ),
            )
            row = connection.execute(
                """
                SELECT * FROM session_inbound_routes
                WHERE channel = ? AND account_id = ? AND sender_id = ?
                  AND message_id = ?
                """,
                (channel, account_id, sender_id, message_id),
            ).fetchone()
        record = dict(row)
        if record["message_hash"] != message_hash:
            raise ValueError("message_id 已绑定到其他普通消息")
        return cursor.rowcount == 1, record

    def finish_session_route(
        self,
        *,
        channel: str,
        account_id: str,
        sender_id: str,
        message_id: str,
        result: dict[str, object],
    ) -> None:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE session_inbound_routes
                SET state = 'completed', result_json = ?, updated_at = ?
                WHERE channel = ? AND account_id = ? AND sender_id = ?
                  AND message_id = ? AND state = 'processing'
                """,
                (
                    json.dumps(result, ensure_ascii=False, sort_keys=True),
                    utc_now(),
                    channel,
                    account_id,
                    sender_id,
                    message_id,
                ),
            )
        if cursor.rowcount != 1:
            raise RuntimeError("普通消息幂等结果无法提交")

    def mark_session_route_unknown(
        self,
        *,
        channel: str,
        account_id: str,
        sender_id: str,
        message_id: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE session_inbound_routes
                SET state = 'unknown', updated_at = ?
                WHERE channel = ? AND account_id = ? AND sender_id = ?
                  AND message_id = ? AND state = 'processing'
                """,
                (utc_now(), channel, account_id, sender_id, message_id),
            )

    def abandon_session_route(
        self,
        *,
        channel: str,
        account_id: str,
        sender_id: str,
        message_id: str,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                DELETE FROM session_inbound_routes
                WHERE channel = ? AND account_id = ? AND sender_id = ?
                  AND message_id = ? AND state = 'processing'
                """,
                (channel, account_id, sender_id, message_id),
            )
