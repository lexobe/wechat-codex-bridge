from __future__ import annotations

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
