from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol
from uuid import uuid4

from .models import DeliveryReceipt, InboundEvent, OutboundRequest, utc_now


class Gateway(Protocol):
    def send(self, request: OutboundRequest) -> DeliveryReceipt: ...


class OpenClawWebhookAdapter:
    """规范化已经通过认证的网关载荷。

    身份认证和签名校验应位于 HTTP 边界。本适配器刻意不包含登录、抓取或
    OpenClaw 连接逻辑。
    """

    @staticmethod
    def normalize(payload: Mapping[str, Any]) -> InboundEvent:
        try:
            return InboundEvent(
                channel=str(payload["channel"]),
                conversation_key=str(
                    payload.get("conversation_key") or payload["contact_key"]
                ),
                sender=str(payload["sender"]),
                body=str(payload.get("body", "")),
                message_id=str(payload["message_id"]),
            )
        except KeyError as exc:
            raise ValueError(f"缺少入站字段：{exc.args[0]}") from exc


@dataclass
class MockGateway:
    """在本地记录发送请求，并返回格式稳定的模拟回执。"""

    sent: list[OutboundRequest] = field(default_factory=list)

    def send(self, request: OutboundRequest) -> DeliveryReceipt:
        self.sent.append(request)
        return DeliveryReceipt(
            request_id=request.request_id,
            channel=request.channel,
            recipient_key=request.recipient_key,
            gateway_receipt_id=f"mock-{uuid4()}",
            status="accepted",
            created_at=utc_now(),
        )
