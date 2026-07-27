import hashlib
import os
import tomllib

import pytest

from wechat_codex_bridge.chrome_runtime import (
    project_chrome_runtime,
    write_agent_codex_config,
)


def write_source_runtime(tmp_path):
    home = tmp_path / "codex"
    plugin = home / "plugins" / "cache" / "openai-bundled" / "chrome" / "1"
    (plugin / "skills" / "control-chrome").mkdir(parents=True)
    (plugin / "scripts").mkdir()
    (plugin / "skills" / "control-chrome" / "SKILL.md").write_text(
        "---\nname: control-chrome\n---\n", encoding="utf-8"
    )
    browser_client = plugin / "scripts" / "browser-client.mjs"
    browser_client.write_text("export {};\n", encoding="utf-8")
    latest = plugin.parent / "latest"
    latest.symlink_to(plugin.name)
    command = tmp_path / "node_repl"
    command.write_text("#!/bin/sh\n", encoding="utf-8")
    command.chmod(0o755)
    (home / "config.toml").write_text(
        "\n".join(
            [
                "[mcp_servers.node_repl]",
                f'command = "{command}"',
                'args = ["--safe"]',
                "[mcp_servers.node_repl.env]",
                'NODE_REPL_NODE_MODULE_DIRS = "/runtime/modules"',
                'NODE_REPL_NODE_PATH = "/runtime/node"',
                'NODE_REPL_TRUSTED_CODE_PATHS = "/不应继承"',
                'BROWSER_USE_AVAILABLE_BACKENDS = "chrome,iab"',
            ]
        ),
        encoding="utf-8",
    )
    return home, plugin, browser_client


def test_projection_only_exposes_same_chrome_runtime(tmp_path):
    home, plugin, browser_client = write_source_runtime(tmp_path)
    agent_home = tmp_path / "agent-codex-home"

    projection = project_chrome_runtime(
        source_codex_home=home,
        agent_codex_home=agent_home,
    )

    env = projection.mcp_server["env"]
    assert projection.control_skill == (
        plugin / "skills" / "control-chrome" / "SKILL.md"
    )
    assert projection.browser_client == browser_client
    assert env["BROWSER_USE_AVAILABLE_BACKENDS"] == "chrome"
    assert env["CODEX_HOME"] == str(agent_home)
    assert env["NODE_REPL_TRUSTED_CODE_PATHS"] == str(plugin)
    assert env["NODE_REPL_TRUSTED_BROWSER_CLIENT_SHA256S"] == hashlib.sha256(
        browser_client.read_bytes()
    ).hexdigest()
    assert "SKY_CUA_SERVICE_PATH" not in env
    assert projection.mcp_server["codex"]["defaultToolsApprovalMode"] == "auto"
    assert os.path.isabs(projection.mcp_server["command"])


def test_projection_fails_closed_without_node_runtime(tmp_path):
    home = tmp_path / "codex"
    home.mkdir()
    (home / "config.toml").write_text("", encoding="utf-8")

    with pytest.raises(ValueError, match="未配置"):
        project_chrome_runtime(
            source_codex_home=home,
            agent_codex_home=tmp_path / "agent",
        )


def test_agent_config_is_atomic_minimal_and_refuses_foreign_file(tmp_path):
    home, plugin, _ = write_source_runtime(tmp_path)
    agent_home = tmp_path / "agent"
    projection = project_chrome_runtime(
        source_codex_home=home,
        agent_codex_home=agent_home,
    )

    target = write_agent_codex_config(
        projection,
        agent_codex_home=agent_home,
    )
    data = tomllib.loads(target.read_text(encoding="utf-8"))
    server = data["mcp_servers"]["node_repl"]
    assert server["command"] == projection.mcp_server["command"]
    assert server["env"]["BROWSER_USE_AVAILABLE_BACKENDS"] == "chrome"
    assert "plugins" not in data
    assert target.stat().st_mode & 0o777 == 0o600

    target.write_text("[model]\nname = 'foreign'\n", encoding="utf-8")
    with pytest.raises(ValueError, match="拒绝覆盖"):
        write_agent_codex_config(
            projection,
            agent_codex_home=agent_home,
        )
