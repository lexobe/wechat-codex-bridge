from __future__ import annotations

from .codex import CodexClient
from .models import InboundEvent, InboundResult
from .store import Store


class Bridge:
    """把规范化入站事件路由到已映射的 Codex 会话。"""

    def __init__(self, store: Store, codex: CodexClient) -> None:
        self.store = store
        self.codex = codex

    def receive(self, event: InboundEvent) -> InboundResult:
        claimed, existing_id = self.store.begin_inbound(
            event.channel, event.message_id, event.conversation_key
        )
        if not claimed:
            if not existing_id:
                raise RuntimeError("已处理的入站事件没有对应会话映射")
            return InboundResult(existing_id, duplicate=True)

        try:
            conversation_id = self.store.get_mapping(
                event.channel, event.conversation_key
            )
            if conversation_id is None:
                proposed_id = self.codex.create_conversation(event)
                conversation_id = self.store.put_mapping(
                    event.channel, event.conversation_key, proposed_id
                )
            self.codex.wake_conversation(conversation_id, event)
            self.store.finish_inbound(
                event.channel, event.message_id, conversation_id
            )
            return InboundResult(conversation_id, duplicate=False)
        except Exception:
            self.store.abandon_inbound(event.channel, event.message_id)
            raise

    def read_only_summary(self, conversation_id: str) -> str:
        """只读操作无需外部影响动作确认即可执行。"""
        return self.codex.summarize(conversation_id)
