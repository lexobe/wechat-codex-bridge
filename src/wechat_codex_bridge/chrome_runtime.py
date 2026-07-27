from __future__ import annotations

import argparse
import hashlib
import json
import os
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

_PASSTHROUGH_ENV = {
    "BROWSER_USE_CODEX_APP_BUILD_FLAVOR",
    "BROWSER_USE_CODEX_APP_VERSION",
    "NODE_REPL_NATIVE_PIPE_CONNECT_TIMEOUT_MS",
    "NODE_REPL_NODE_MODULE_DIRS",
    "NODE_REPL_NODE_PATH",
}
_MANAGED_CONFIG_MARKER = "# 由 wechat-codex-bridge 管理：隔离 Chrome 运行时\n"


@dataclass(frozen=True, slots=True)
class ChromeRuntimeProjection:
    """从 Codex Desktop 提取的最小 Chrome 运行时投影。"""

    mcp_server: dict[str, object]
    control_skill: Path
    browser_client: Path

    def as_dict(self) -> dict[str, object]:
        return {
            "mcp_server": self.mcp_server,
            "control_skill": str(self.control_skill),
            "browser_client": str(self.browser_client),
        }


def project_chrome_runtime(
    *,
    source_codex_home: str | Path,
    agent_codex_home: str | Path,
) -> ChromeRuntimeProjection:
    source_home = Path(source_codex_home).expanduser().resolve(strict=True)
    agent_home = Path(agent_codex_home).expanduser().resolve(strict=False)
    config_path = source_home / "config.toml"
    with config_path.open("rb") as source:
        config = tomllib.load(source)

    server = config.get("mcp_servers", {}).get("node_repl")
    if not isinstance(server, dict):
        raise ValueError("Codex Desktop 未配置 Chrome 所需的浏览器控制服务")
    command = Path(str(server.get("command", ""))).expanduser()
    if not command.is_absolute() or not command.is_file():
        raise ValueError("Codex Desktop 的浏览器控制命令无效")
    if not os.access(command, os.X_OK):
        raise ValueError("Codex Desktop 的浏览器控制命令不可执行")

    args = server.get("args", [])
    if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
        raise ValueError("Codex Desktop 的浏览器控制参数无效")
    source_env = server.get("env", {})
    if not isinstance(source_env, dict):
        raise ValueError("Codex Desktop 的浏览器控制环境无效")

    plugin_root = (
        source_home / "plugins" / "cache" / "openai-bundled" / "chrome" / "latest"
    ).resolve(strict=True)
    control_skill = plugin_root / "skills" / "control-chrome" / "SKILL.md"
    browser_client = plugin_root / "scripts" / "browser-client.mjs"
    if not control_skill.is_file() or not browser_client.is_file():
        raise ValueError("Codex Desktop 的 Chrome 插件不完整")

    digest = hashlib.sha256(browser_client.read_bytes()).hexdigest()
    env = {
        key: value
        for key in _PASSTHROUGH_ENV
        if isinstance((value := source_env.get(key)), str) and value
    }
    required_env = {
        "NODE_REPL_NODE_MODULE_DIRS",
        "NODE_REPL_NODE_PATH",
    }
    missing = sorted(required_env - env.keys())
    if missing:
        raise ValueError(f"Codex Desktop 浏览器环境缺少：{', '.join(missing)}")
    env.update(
        {
            "BROWSER_USE_AVAILABLE_BACKENDS": "chrome",
            "CODEX_HOME": str(agent_home),
            "NODE_REPL_INSTRUCTIONS_USE_CASE_CHROME": (
                "只通过 Chrome 插件复用用户已登录的同一个 Chrome。"
            ),
            "NODE_REPL_TRUSTED_BROWSER_CLIENT_SHA256S": digest,
            "NODE_REPL_TRUSTED_CODE_PATHS": str(plugin_root),
        }
    )
    return ChromeRuntimeProjection(
        mcp_server={
            "command": str(command),
            "args": args,
            "env": env,
            "enabled": True,
            "codex": {"defaultToolsApprovalMode": "auto"},
        },
        control_skill=control_skill,
        browser_client=browser_client,
    )


def write_agent_codex_config(
    projection: ChromeRuntimeProjection,
    *,
    agent_codex_home: str | Path,
) -> Path:
    """原子写入隔离 Codex Home；已有非本组件配置时拒绝覆盖。"""

    agent_home = Path(agent_codex_home).expanduser().resolve(strict=False)
    agent_home.mkdir(parents=True, exist_ok=True)
    target = agent_home / "config.toml"
    if target.exists():
        current = target.read_text(encoding="utf-8")
        if not current.startswith(_MANAGED_CONFIG_MARKER):
            raise ValueError("隔离 Codex Home 已有非本组件配置，拒绝覆盖")

    server = projection.mcp_server
    env = server.get("env")
    if not isinstance(env, dict):
        raise ValueError("Chrome MCP 环境无效")
    args = server.get("args")
    if not isinstance(args, list):
        raise ValueError("Chrome MCP 参数无效")
    lines = [
        _MANAGED_CONFIG_MARKER.rstrip("\n"),
        "[mcp_servers.node_repl]",
        f"command = {_toml_string(str(server['command']))}",
        f"args = [{', '.join(_toml_string(str(item)) for item in args)}]",
        "startup_timeout_sec = 120",
        'default_tools_approval_mode = "auto"',
        "",
        "[mcp_servers.node_repl.env]",
    ]
    lines.extend(
        f"{key} = {_toml_string(str(value))}"
        for key, value in sorted(env.items())
    )
    content = "\n".join(lines) + "\n"
    file_descriptor, temporary_name = tempfile.mkstemp(
        prefix=".config.toml.",
        dir=agent_home,
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(file_descriptor, "w", encoding="utf-8") as sink:
            sink.write(content)
            sink.flush()
            os.fsync(sink.fileno())
        temporary.chmod(0o600)
        temporary.replace(target)
    except Exception:
        temporary.unlink(missing_ok=True)
        raise
    return target


def _toml_string(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="生成隔离 Codex 的 Chrome 运行时投影")
    parser.add_argument("--source-codex-home", required=True)
    parser.add_argument("--agent-codex-home", required=True)
    parser.add_argument("--write-agent-config", action="store_true")
    arguments = parser.parse_args(argv)
    projection = project_chrome_runtime(
        source_codex_home=arguments.source_codex_home,
        agent_codex_home=arguments.agent_codex_home,
    )
    if arguments.write_agent_config:
        write_agent_codex_config(
            projection,
            agent_codex_home=arguments.agent_codex_home,
        )
    print(json.dumps(projection.as_dict(), ensure_ascii=False, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
