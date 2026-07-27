import json
import subprocess

import pytest

from wechat_codex_bridge.codex_provider import CodexAppServerProvider
from wechat_codex_bridge.session_models import ResumeFailed, TurnStatus

THREAD_ID = "019f9bf4-fee1-7012-b571-243ea4000269"


def make_provider(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    runner = tmp_path / "runner.mjs"
    runner.write_text("", encoding="utf-8")
    plugin = tmp_path / "codex-plugin"
    plugin.mkdir()
    config = tmp_path / "openclaw.json"
    config.write_text("{}", encoding="utf-8")
    agent = tmp_path / "agent"
    agent.mkdir()
    skill = root / "skills" / "x-twitter-chrome" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: x-twitter-chrome\n---\n", encoding="utf-8")
    chrome_skill = tmp_path / "chrome" / "skills" / "control-chrome" / "SKILL.md"
    chrome_skill.parent.mkdir(parents=True)
    chrome_skill.write_text("---\nname: control-chrome\n---\n", encoding="utf-8")
    browser_client = tmp_path / "chrome" / "scripts" / "browser-client.mjs"
    browser_client.parent.mkdir(parents=True)
    browser_client.write_text("export {};\n", encoding="utf-8")
    provider = CodexAppServerProvider(
        runner=runner,
        codex_plugin_root=plugin,
        openclaw_config=config,
        openclaw_agent_dir=agent,
        root=root,
        required_codex_skill=skill,
        chrome_codex_skill=chrome_skill,
        chrome_browser_client=browser_client,
    )
    return root, provider


def completed(result, returncode=0):
    return subprocess.CompletedProcess(
        args=[],
        returncode=returncode,
        stdout=json.dumps({"ok": returncode == 0, "result": result}) + "\n",
        stderr="",
    )


def test_codex_provider_create_resume_and_turn_contract(tmp_path, monkeypatch):
    root, provider = make_provider(tmp_path)
    directory = root / "客户" / "项目"
    directory.mkdir(parents=True)
    calls = []

    def fake_run(*args, **kwargs):
        request = json.loads(kwargs["input"])
        calls.append(request)
        if request["op"] == "probe":
            return completed(
                {
                    "provider_admitted": True,
                    "same_chrome_read_access": True,
                }
            )
        if request["op"] in {"create", "resume"}:
            return completed({"session_id": THREAD_ID, "cwd": str(directory)})
        return completed(
            {"status": "accepted_completed", "reply": "真实 Codex 回复"}
        )

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert provider.probe() == {
        "provider_admitted": True,
        "same_chrome_read_access": True,
    }
    assert provider.capabilities().cross_process_resume is True
    session_id = provider.create_session(
        directory, body=None, idempotency_key="create-1"
    )
    prepared = provider.prepare_resume(session_id, directory)
    receipt = provider.run_turn(
        prepared, "继续处理", idempotency_key="message-1"
    )

    assert session_id == THREAD_ID
    assert receipt.status is TurnStatus.ACCEPTED_COMPLETED
    assert receipt.reply == "真实 Codex 回复"
    assert [call["op"] for call in calls] == ["probe", "create", "resume", "turn"]
    assert calls[-1]["required_codex_skill"].endswith(
        "skills/x-twitter-chrome/SKILL.md"
    )
    assert calls[-1]["chrome_codex_skill"].endswith(
        "skills/control-chrome/SKILL.md"
    )


def test_codex_provider_rejects_body_create_and_resume_mismatch(
    tmp_path, monkeypatch
):
    root, provider = make_provider(tmp_path)
    directory = root / "项目"
    directory.mkdir()

    with pytest.raises(ValueError, match="无正文"):
        provider.create_session(
            directory, body="不得携带正文", idempotency_key="create-1"
        )

    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *args, **kwargs: completed(
            {
                "session_id": "019f9bf4-fee1-7012-b571-243ea4000270",
                "cwd": str(directory),
            }
        ),
    )
    with pytest.raises(ResumeFailed):
        provider.prepare_resume(THREAD_ID, directory)
