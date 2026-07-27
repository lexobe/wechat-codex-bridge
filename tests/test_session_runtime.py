import io
import json

import pytest

from wechat_codex_bridge.session_models import (
    ProviderNotAdmitted,
    RouteOutcome,
    RouteResult,
    TurnReceipt,
    TurnStatus,
)
from wechat_codex_bridge.session_provider import MockSessionProvider
from wechat_codex_bridge.session_runtime import (
    SessionRuntimeService,
    probe_runtime,
    run_contract_probe,
    serve,
)


def make_skill(root):
    skill = root / "skills" / "x-twitter-chrome" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: x-twitter-chrome\n---\n", encoding="utf-8")
    return skill


def test_disabled_runtime_reports_x_probe_fail_closed(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    skill = make_skill(root)
    probe = probe_runtime("disabled", skill, root)

    assert probe.skill_discoverable is True
    assert probe.provider_admitted is False
    assert probe.same_chrome_read_access is False
    assert probe.confirmed_x_writes is False
    assert probe.ready is False
    assert probe.x_ready is False


def test_live_runtime_never_registers_mock_and_create_does_not_touch_directory(
    tmp_path,
):
    root = tmp_path / "root"
    root.mkdir()
    skill = make_skill(root)
    service = SessionRuntimeService(
        root=root,
        db_path=tmp_path / "session.db",
        provider_mode="disabled",
        required_skill=skill,
    )

    with pytest.raises(ProviderNotAdmitted):
        service.dispatch(
            {
                "op": "route",
                "channel": "openclaw-weixin",
                "account_id": "main",
                "sender_id": "owner",
                "message_id": "create-1",
                "body": "@不会创建|create",
            }
        )

    assert not (root / "不会创建").exists()


def test_json_lines_runtime_returns_safe_structured_error(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    skill = make_skill(root)
    service = SessionRuntimeService(
        root=root,
        db_path=tmp_path / "session.db",
        provider_mode="disabled",
        required_skill=skill,
    )
    source = io.StringIO(
        json.dumps(
            {
                "id": 7,
                "op": "route",
                "channel": "openclaw-weixin",
                "account_id": "main",
                "sender_id": "owner",
                "message_id": "m-1",
                "body": " @项目 正文",
            },
            ensure_ascii=False,
        )
        + "\n"
    )
    sink = io.StringIO()

    serve(service, source, sink)

    response = json.loads(sink.getvalue())
    assert response["id"] == 7
    assert response["ok"] is False
    assert response["error"]["code"] == "ProtocolError"
    assert "字符位置 0" in response["error"]["message"]


def test_runtime_preserves_provider_turn_status():
    class FailedRouter:
        @staticmethod
        def handle(inbound):
            return RouteResult(
                outcome=RouteOutcome.DELIVERED,
                provider="codex",
                session_id="session-1",
                directory="/safe/project",
                turn_status=TurnStatus.ACCEPTED_FAILED,
            )

    service = object.__new__(SessionRuntimeService)
    service.router = FailedRouter()

    result = service.dispatch(
        {
            "op": "route",
            "channel": "openclaw-weixin",
            "account_id": "main",
            "sender_id": "owner",
            "message_id": "m-failed",
            "body": "@项目 正文",
        }
    )

    assert result["turn_status"] == "accepted_failed"
    assert result["reply"] is None


def test_contract_probe_really_creates_resumes_and_runs_turn(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    provider = MockSessionProvider(name="codex")
    provider.probe = lambda: {
        "provider_admitted": True,
        "same_chrome_read_access": True,
    }
    original_run_turn = provider.run_turn

    def run_turn(prepared, body, *, idempotency_key):
        original_run_turn(
            prepared,
            body,
            idempotency_key=idempotency_key,
        )
        return TurnReceipt(
            TurnStatus.ACCEPTED_COMPLETED,
            "SESSION_CHAT_CONTRACT_OK",
        )

    provider.run_turn = run_turn

    result = run_contract_probe(provider, root)

    assert result["passed"] is True
    assert result["turn_status"] == "accepted_completed"
    assert len(provider.create_calls) == 1
    assert len(provider.turn_calls) == 1
    assert (root / ".session-chat-contract-probe").is_dir()


def test_contract_probe_rejects_symlink_directory(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / ".session-chat-contract-probe").symlink_to(outside)
    provider = MockSessionProvider(name="codex")

    with pytest.raises(ValueError, match="符号链接"):
        run_contract_probe(provider, root)

    assert provider.create_calls == []
