from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _required(value: str, field: str) -> str:
    value = value.strip()
    if not value:
        raise ValueError(f"{field} 不能为空")
    return value


@dataclass(frozen=True, slots=True)
class InboundEvent:
    channel: str
    conversation_key: str
    sender: str
    body: str
    message_id: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "channel", _required(self.channel, "channel"))
        object.__setattr__(
            self, "conversation_key", _required(self.conversation_key, "conversation_key")
        )
        object.__setattr__(self, "sender", _required(self.sender, "sender"))
        object.__setattr__(self, "message_id", _required(self.message_id, "message_id"))
        if not isinstance(self.body, str):
            raise ValueError("body 必须是字符串")


@dataclass(frozen=True, slots=True)
class InboundResult:
    codex_conversation_id: str
    duplicate: bool


class OutboundPurpose(StrEnum):
    MESSAGE = "message"
    REMINDER = "reminder"


@dataclass(frozen=True, slots=True)
class OutboundRequest:
    request_id: str
    channel: str
    recipient_key: str
    body: str
    purpose: OutboundPurpose = OutboundPurpose.MESSAGE
    confirmation_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "request_id", _required(self.request_id, "request_id"))
        object.__setattr__(self, "channel", _required(self.channel, "channel"))
        object.__setattr__(
            self, "recipient_key", _required(self.recipient_key, "recipient_key")
        )
        object.__setattr__(self, "body", _required(self.body, "body"))
        if isinstance(self.purpose, str):
            object.__setattr__(self, "purpose", OutboundPurpose(self.purpose))


@dataclass(frozen=True, slots=True)
class DeliveryReceipt:
    request_id: str
    channel: str
    recipient_key: str
    gateway_receipt_id: str
    status: str
    created_at: str


class ConfirmationState(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    EXECUTING = "executing"
    CONSUMED = "consumed"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class PendingAction:
    confirmation_id: str
    action_kind: str
    target_key: str
    state: ConfirmationState
    created_at: str
