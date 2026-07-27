from dataclasses import replace

import pytest

from wechat_codex_bridge import ControlledSendTool, MockGateway, OutboundRequest, Store
from wechat_codex_bridge.mcp import ConfirmationRequired, RecipientNotAuthorized


def make_tool(tmp_path):
    gateway = MockGateway()
    tool = ControlledSendTool(
        Store(tmp_path / "bridge.db"), gateway, {"contact:alice"}
    )
    return tool, gateway


def test_rejects_recipient_outside_allowlist(tmp_path):
    tool, gateway = make_tool(tmp_path)
    request = OutboundRequest("r-1", "wechat", "contact:mallory", "你好")

    with pytest.raises(RecipientNotAuthorized):
        tool.request_confirmation(request)
    with pytest.raises(RecipientNotAuthorized):
        tool.send(request)

    assert gateway.sent == []


def test_send_requires_confirmation_bound_to_exact_payload(tmp_path):
    tool, gateway = make_tool(tmp_path)
    request = OutboundRequest("r-1", "wechat", "contact:alice", "你好")

    with pytest.raises(ConfirmationRequired):
        tool.send(request)

    pending = tool.request_confirmation(request)
    tool.approve(pending.confirmation_id)

    with pytest.raises(ConfirmationRequired):
        tool.send(
            replace(
                request,
                body="批准后修改的内容",
                confirmation_id=pending.confirmation_id,
            )
        )

    receipt = tool.send(replace(request, confirmation_id=pending.confirmation_id))
    assert receipt.status == "accepted"
    assert len(gateway.sent) == 1


def test_approved_reminder_and_request_id_are_idempotent(tmp_path):
    tool, gateway = make_tool(tmp_path)
    request = OutboundRequest(
        "reminder-1",
        "wechat",
        "contact:alice",
        "已批准的提醒",
        purpose="reminder",
    )
    pending = tool.request_confirmation(request)
    tool.approve(pending.confirmation_id)
    approved = replace(request, confirmation_id=pending.confirmation_id)

    first = tool.send(approved)
    duplicate = tool.send(approved)

    assert duplicate == first
    assert len(gateway.sent) == 1

    with pytest.raises(ValueError, match="其他载荷"):
        tool.send(replace(approved, body="复用请求编号时修改内容"))


def test_ambiguous_gateway_failure_does_not_restore_approval(tmp_path):
    class AmbiguousGateway:
        def __init__(self):
            self.calls = 0

        def send(self, request):
            self.calls += 1
            raise TimeoutError("发送后回执丢失")

    gateway = AmbiguousGateway()
    tool = ControlledSendTool(Store(tmp_path / "bridge.db"), gateway, {"contact:alice"})
    request = OutboundRequest("r-unknown", "wechat", "contact:alice", "只发送一次")
    pending = tool.request_confirmation(request)
    tool.approve(pending.confirmation_id)
    approved = replace(request, confirmation_id=pending.confirmation_id)

    with pytest.raises(TimeoutError, match="回执丢失"):
        tool.send(approved)
    with pytest.raises(ConfirmationRequired):
        tool.send(approved)

    assert gateway.calls == 1
