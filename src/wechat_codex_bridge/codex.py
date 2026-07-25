from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from .models import InboundEvent


class CodexClient(Protocol):
    """桥接层对未来 Codex 适配器所需的最小接口。"""

    def create_conversation(self, event: InboundEvent) -> str: ...

    def wake_conversation(self, conversation_id: str, event: InboundEvent) -> None: ...

    def summarize(self, conversation_id: str) -> str: ...


@dataclass
class MockCodexClient:
    """用于测试和演示的内存适配器；不会访问网络或账号。"""

    created: list[str] = field(default_factory=list)
    wakes: list[tuple[str, InboundEvent]] = field(default_factory=list)
    _counter: int = 0

    def create_conversation(self, event: InboundEvent) -> str:
        self._counter += 1
        conversation_id = f"codex-local-{self._counter}"
        self.created.append(conversation_id)
        return conversation_id

    def wake_conversation(self, conversation_id: str, event: InboundEvent) -> None:
        self.wakes.append((conversation_id, event))

    def summarize(self, conversation_id: str) -> str:
        count = sum(item[0] == conversation_id for item in self.wakes)
        return f"本地只读摘要：{conversation_id} 包含 {count} 个入站事件。"
