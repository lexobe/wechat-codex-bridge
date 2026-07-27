from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path


class SessionChatError(RuntimeError):
    """session-chat 对外统一错误基类。"""


class ProtocolError(SessionChatError):
    pass


class PathSecurityError(SessionChatError):
    pass


class BindingInvalid(SessionChatError):
    pass


class ProviderNotAdmitted(SessionChatError):
    pass


class ResumeFailed(SessionChatError):
    pass


class NoActiveSession(SessionChatError):
    pass


class ControlOperationBlocked(SessionChatError):
    pass


class UnknownAfterProviderCreate(SessionChatError):
    pass


class DuplicateMessageBlocked(SessionChatError):
    pass


class MessageKind(StrEnum):
    ORDINARY = "ordinary"
    NEW = "new"
    CREATE = "create"


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    kind: MessageKind
    raw_path: str
    body: str | None = None


@dataclass(frozen=True, slots=True)
class SenderKey:
    channel: str
    account_id: str
    sender_id: str


@dataclass(frozen=True, slots=True)
class SessionInbound:
    sender: SenderKey
    message_id: str
    body: str


@dataclass(frozen=True, slots=True)
class Binding:
    schema_version: int
    provider: str
    session_id: str


class BindingState(StrEnum):
    ABSENT = "absent"
    VALID = "valid"
    INVALID = "invalid"


@dataclass(frozen=True, slots=True)
class BindingRead:
    state: BindingState
    binding: Binding | None = None
    reason: str | None = None


@dataclass(frozen=True, slots=True)
class ActiveSession:
    directory: Path
    provider: str
    session_id: str


class TurnStatus(StrEnum):
    NOT_ACCEPTED = "not_accepted"
    ACCEPTED_COMPLETED = "accepted_completed"
    ACCEPTED_FAILED = "accepted_failed"
    ACCEPTED_UNKNOWN = "accepted_unknown"


@dataclass(frozen=True, slots=True)
class TurnReceipt:
    status: TurnStatus
    reply: str | None = None


class RouteOutcome(StrEnum):
    DELIVERED = "delivered"
    FALLBACK_TO_ACTIVE = "fallback_to_active"
    SESSION_CREATED = "session_created"
    DIRECTORY_CREATED = "directory_created"


@dataclass(frozen=True, slots=True)
class RouteResult:
    outcome: RouteOutcome
    provider: str
    session_id: str
    directory: str
    turn_status: TurnStatus | None = None
    reply: str | None = None
    duplicate: bool = False


@dataclass(frozen=True, slots=True)
class ProviderCapabilities:
    create_without_body: bool
    stable_session_id: bool
    cross_process_resume: bool
    stable_cwd: bool
    structured_turn_status: bool
    long_lived_session: bool
    workspace_skill_discovery: bool = False
    same_chrome_read_access: bool = False
    confirmed_x_writes: bool = False

    @property
    def mvp_ready(self) -> bool:
        return all(
            (
                self.create_without_body,
                self.stable_session_id,
                self.cross_process_resume,
                self.stable_cwd,
                self.structured_turn_status,
                self.long_lived_session,
            )
        )

    @property
    def codex_x_ready(self) -> bool:
        """X 完整能力状态；不等同于核心目录会话 provider 准入。"""

        return all(
            (
                self.workspace_skill_discovery,
                self.same_chrome_read_access,
                self.confirmed_x_writes,
            )
        )
