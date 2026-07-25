import pytest

from wechat_codex_bridge import OpenClawWebhookAdapter


def test_mock_openclaw_payload_is_normalized():
    event = OpenClawWebhookAdapter.normalize(
        {
            "channel": "wechat",
            "contact_key": "contact:alice",
            "sender": "alice",
            "body": "你好",
            "message_id": "m-1",
        }
    )
    assert event.conversation_key == "contact:alice"
    assert event.message_id == "m-1"


def test_missing_idempotency_id_is_rejected():
    with pytest.raises(ValueError, match="message_id"):
        OpenClawWebhookAdapter.normalize(
            {
                "channel": "wechat",
                "contact_key": "contact:alice",
                "sender": "alice",
                "body": "你好",
            }
        )
