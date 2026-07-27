from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .session_models import (
    ProviderCapabilities,
    ResumeFailed,
    TurnReceipt,
    TurnStatus,
)


@dataclass(frozen=True, slots=True)
class PreparedSession:
    provider: str
    session_id: str
    cwd: Path


class SessionProvider(Protocol):
    name: str

    def capabilities(self) -> ProviderCapabilities:
        ...

    def validate_session_id(self, session_id: str) -> bool:
        ...

    def create_session(self, cwd: Path, *, body: None, idempotency_key: str) -> str:
        ...

    def prepare_resume(self, session_id: str, cwd: Path) -> PreparedSession:
        ...

    def run_turn(
        self, prepared: PreparedSession, body: str, *, idempotency_key: str
    ) -> TurnReceipt:
        ...


@dataclass
class MockSessionProvider:
    """完全本地的 provider 合同实现，不访问网络、账号或真实运行时。"""

    name: str = "mock"
    sessions: dict[str, tuple[Path, list[str]]] = field(default_factory=dict)
    create_calls: list[tuple[Path, str]] = field(default_factory=list)
    turn_calls: list[tuple[str, str, str]] = field(default_factory=list)
    fail_create: bool = False
    fail_resume: bool = False
    fail_turn: bool = False
    _counter: int = 0

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            create_without_body=True,
            stable_session_id=True,
            cross_process_resume=True,
            stable_cwd=True,
            structured_turn_status=True,
            long_lived_session=True,
        )

    def validate_session_id(self, session_id: str) -> bool:
        return bool(session_id) and session_id.startswith(f"{self.name}-session-")

    def create_session(self, cwd: Path, *, body: None, idempotency_key: str) -> str:
        if body is not None:
            raise ValueError("mock provider 只允许无正文创建 session")
        if self.fail_create:
            raise RuntimeError("模拟 provider 创建失败")
        self._counter += 1
        session_id = f"{self.name}-session-{self._counter}"
        canonical = cwd.resolve(strict=True)
        self.sessions[session_id] = (canonical, [])
        self.create_calls.append((canonical, idempotency_key))
        return session_id

    def prepare_resume(self, session_id: str, cwd: Path) -> PreparedSession:
        if self.fail_resume or session_id not in self.sessions:
            raise ResumeFailed("provider 无法恢复指定 session")
        canonical = cwd.resolve(strict=True)
        stored_cwd, _ = self.sessions[session_id]
        if canonical != stored_cwd:
            raise ResumeFailed("provider session 的 cwd 与目标目录不一致")
        return PreparedSession(self.name, session_id, canonical)

    def run_turn(
        self, prepared: PreparedSession, body: str, *, idempotency_key: str
    ) -> TurnReceipt:
        if self.fail_turn:
            raise RuntimeError("模拟 provider turn 结果未知")
        stored_cwd, turns = self.sessions[prepared.session_id]
        if stored_cwd != prepared.cwd:
            return TurnReceipt(TurnStatus.NOT_ACCEPTED)
        turns.append(body)
        self.turn_calls.append((prepared.session_id, body, idempotency_key))
        return TurnReceipt(
            TurnStatus.ACCEPTED_COMPLETED,
            reply=f"mock 已处理：{body}",
        )
