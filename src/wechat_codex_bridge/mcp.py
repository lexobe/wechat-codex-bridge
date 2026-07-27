from __future__ import annotations

import hashlib
import json
from uuid import uuid4

from .gateway import Gateway
from .models import (
    ConfirmationState,
    DeliveryReceipt,
    OutboundPurpose,
    OutboundRequest,
    PendingAction,
)
from .store import Store


class RecipientNotAuthorized(PermissionError):
    pass


class ConfirmationRequired(PermissionError):
    pass


def request_hash(request: OutboundRequest) -> str:
    canonical = json.dumps(
        {
            "channel": request.channel,
            "recipient_key": request.recipient_key,
            "body": request.body,
            "purpose": request.purpose.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


class ControlledSendTool:
    """带收件人授权和显式批准的 MCP 风格 `wechat.send_message` 工具。"""

    name = "wechat.send_message"
    input_schema = {
        "type": "object",
        "required": [
            "request_id",
            "channel",
            "recipient_key",
            "body",
            "confirmation_id",
        ],
        "properties": {
            "request_id": {"type": "string"},
            "channel": {"type": "string"},
            "recipient_key": {"type": "string"},
            "body": {"type": "string"},
            "purpose": {"enum": ["message", "reminder"]},
            "confirmation_id": {"type": "string"},
        },
        "additionalProperties": False,
    }

    def __init__(
        self, store: Store, gateway: Gateway, allowed_recipients: set[str]
    ) -> None:
        self.store = store
        self.gateway = gateway
        self.allowed_recipients = frozenset(allowed_recipients)

    def request_confirmation(self, request: OutboundRequest) -> PendingAction:
        self._authorize_recipient(request.recipient_key)
        confirmation_id = str(uuid4())
        return self.store.create_confirmation(
            confirmation_id=confirmation_id,
            action_kind=f"send:{request.purpose.value}",
            target_key=request.recipient_key,
            payload_hash=request_hash(request),
        )

    def approve(self, confirmation_id: str) -> None:
        if not self.store.set_confirmation_state(
            confirmation_id,
            ConfirmationState.PENDING.value,
            ConfirmationState.APPROVED.value,
        ):
            raise ValueError("确认记录不存在或不处于待确认状态")

    def reject(self, confirmation_id: str) -> None:
        if not self.store.set_confirmation_state(
            confirmation_id,
            ConfirmationState.PENDING.value,
            ConfirmationState.REJECTED.value,
        ):
            raise ValueError("确认记录不存在或不处于待确认状态")

    def call(self, arguments: dict[str, object]) -> dict[str, str]:
        request_id = arguments.get("request_id")
        channel = arguments.get("channel")
        recipient_key = arguments.get("recipient_key")
        body = arguments.get("body")
        purpose = arguments.get("purpose", OutboundPurpose.MESSAGE.value)
        confirmation_id = arguments.get("confirmation_id")
        required = {
            "request_id": request_id,
            "channel": channel,
            "recipient_key": recipient_key,
            "body": body,
            "purpose": purpose,
            "confirmation_id": confirmation_id,
        }
        invalid = [
            name
            for name, value in required.items()
            if not isinstance(value, str) or not value
        ]
        if invalid:
            raise ValueError(f"发送参数必须是非空字符串：{', '.join(invalid)}")
        assert isinstance(request_id, str)
        assert isinstance(channel, str)
        assert isinstance(recipient_key, str)
        assert isinstance(body, str)
        assert isinstance(purpose, str)
        assert isinstance(confirmation_id, str)
        request = OutboundRequest(
            request_id=request_id,
            channel=channel,
            recipient_key=recipient_key,
            body=body,
            purpose=OutboundPurpose(purpose),
            confirmation_id=confirmation_id,
        )
        receipt = self.send(request)
        return {
            "request_id": receipt.request_id,
            "recipient_key": receipt.recipient_key,
            "gateway_receipt_id": receipt.gateway_receipt_id,
            "status": receipt.status,
            "created_at": receipt.created_at,
        }

    def send(self, request: OutboundRequest) -> DeliveryReceipt:
        existing = self.store.get_receipt(request.request_id)
        if existing is not None:
            if self.store.receipt_payload_hash(request.request_id) != request_hash(
                request
            ):
                raise ValueError("request_id 已被其他载荷使用")
            return existing

        self._authorize_recipient(request.recipient_key)
        payload_hash = request_hash(request)
        if request.confirmation_id is None or not self.store.claim_confirmation(
            request.confirmation_id, request, payload_hash
        ):
            raise ConfirmationRequired(
                "必须提供与本次发送内容完全绑定且已经批准的确认记录"
            )

        try:
            receipt = self.gateway.send(request)
            self.store.save_receipt(receipt, payload_hash)
        except Exception:
            # 网关可能已经完成外发，只是回执在返回途中丢失。保留 executing
            # 作为需要人工核对的未知状态，禁止用同一批准自动重试。
            raise

        self.store.set_confirmation_state(
            request.confirmation_id,
            ConfirmationState.EXECUTING.value,
            ConfirmationState.CONSUMED.value,
        )
        return receipt

    def _authorize_recipient(self, recipient_key: str) -> None:
        if recipient_key not in self.allowed_recipients:
            raise RecipientNotAuthorized(f"收件人未获授权：{recipient_key}")
