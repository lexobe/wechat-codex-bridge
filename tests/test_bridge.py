from wechat_codex_bridge import Bridge, InboundEvent, MockCodexClient, Store


def test_duplicate_inbound_message_is_idempotent(tmp_path):
    store = Store(tmp_path / "bridge.db")
    codex = MockCodexClient()
    bridge = Bridge(store, codex)
    event = InboundEvent(
        channel="wechat",
        conversation_key="contact:alice",
        sender="alice",
        body="你好",
        message_id="wx-message-1",
    )

    first = bridge.receive(event)
    duplicate = bridge.receive(event)

    assert first.duplicate is False
    assert duplicate.duplicate is True
    assert duplicate.codex_conversation_id == first.codex_conversation_id
    assert len(codex.created) == 1
    assert len(codex.wakes) == 1


def test_new_message_wakes_existing_mapping(tmp_path):
    store = Store(tmp_path / "bridge.db")
    codex = MockCodexClient()
    bridge = Bridge(store, codex)

    first = bridge.receive(
        InboundEvent("wechat", "contact:alice", "alice", "第一条", "m-1")
    )
    second = bridge.receive(
        InboundEvent("wechat", "contact:alice", "alice", "第二条", "m-2")
    )

    assert first.codex_conversation_id == second.codex_conversation_id
    assert len(codex.created) == 1
    assert len(codex.wakes) == 2


def test_read_only_summary_needs_no_confirmation(tmp_path):
    bridge = Bridge(Store(tmp_path / "bridge.db"), MockCodexClient())
    assert "本地只读摘要" in bridge.read_only_summary("codex-local-1")
