from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .session_models import ProviderCapabilities, ResumeFailed, TurnReceipt, TurnStatus
from .session_provider import PreparedSession

_THREAD_ID = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


@dataclass
class CodexAppServerProvider:
    """经 OpenClaw 官方认证桥调用 Codex App Server 的真实 provider。"""

    runner: Path
    codex_plugin_root: Path
    openclaw_config: Path
    openclaw_agent_dir: Path
    root: Path
    required_codex_skill: Path
    chrome_codex_skill: Path
    chrome_browser_client: Path
    node_path: str = "node"
    model: str = "gpt-5.6-sol"
    request_timeout_seconds: int = 60
    turn_timeout_ms: int = 300_000
    name: str = "codex"

    def __post_init__(self) -> None:
        for path, label in (
            (self.runner, "Codex provider runner"),
            (self.codex_plugin_root, "Codex 插件目录"),
            (self.openclaw_config, "OpenClaw 配置"),
            (self.openclaw_agent_dir, "OpenClaw agent 目录"),
            (self.root, "工作根目录"),
            (self.required_codex_skill, "X 技能"),
            (self.chrome_codex_skill, "Chrome 控制技能"),
            (self.chrome_browser_client, "Chrome 浏览器客户端"),
        ):
            if not path.exists():
                raise ValueError(f"{label}不存在：{path}")

    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            create_without_body=True,
            stable_session_id=True,
            # 纯 thread/start 只建内存对象；runner 随后用不含用户正文的
            # thread/name/set 提交空 thread。真实探针已验证新进程可按 ID 恢复。
            cross_process_resume=True,
            stable_cwd=True,
            structured_turn_status=True,
            long_lived_session=True,
            workspace_skill_discovery=True,
            same_chrome_read_access=True,
            confirmed_x_writes=False,
        )

    def validate_session_id(self, session_id: str) -> bool:
        return bool(_THREAD_ID.fullmatch(session_id))

    def _request(self, **payload: object) -> dict[str, object]:
        request = {
            **payload,
            "codex_plugin_root": str(self.codex_plugin_root),
            "openclaw_config": str(self.openclaw_config),
            "openclaw_agent_dir": str(self.openclaw_agent_dir),
            "root": str(self.root),
            "required_codex_skill": str(self.required_codex_skill),
            "chrome_codex_skill": str(self.chrome_codex_skill),
            "chrome_browser_client": str(self.chrome_browser_client),
            "model": self.model,
            "turn_timeout_ms": self.turn_timeout_ms,
        }
        completed = subprocess.run(
            [self.node_path, str(self.runner)],
            input=json.dumps(request, ensure_ascii=False),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=max(self.request_timeout_seconds, self.turn_timeout_ms // 1000 + 30),
            check=False,
        )
        lines = [line for line in completed.stdout.splitlines() if line.strip()]
        if not lines:
            detail = completed.stderr.strip()[-800:]
            raise RuntimeError(f"Codex provider 没有返回结构化结果：{detail}")
        try:
            response = json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise RuntimeError("Codex provider 返回了无效 JSON") from exc
        if completed.returncode != 0 or response.get("ok") is not True:
            message = response.get("error") or completed.stderr.strip()[-800:]
            raise RuntimeError(f"Codex provider 失败：{message}")
        result = response.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("Codex provider 缺少结果对象")
        return result

    def probe(self) -> dict[str, bool]:
        result = self._request(op="probe")
        return {
            "provider_admitted": result.get("provider_admitted") is True,
            "same_chrome_read_access": (
                result.get("same_chrome_read_access") is True
            ),
        }

    def create_session(self, cwd: Path, *, body: None, idempotency_key: str) -> str:
        if body is not None:
            raise ValueError("Codex provider 只允许无正文创建 session")
        canonical = cwd.resolve(strict=True)
        result = self._request(
            op="create",
            cwd=str(canonical),
            idempotency_key=idempotency_key,
        )
        session_id = str(result.get("session_id", ""))
        if not self.validate_session_id(session_id):
            raise RuntimeError("Codex provider 返回了无效 thread ID")
        if Path(str(result.get("cwd", ""))).resolve(strict=True) != canonical:
            raise RuntimeError("Codex provider 创建后的 cwd 不一致")
        return session_id

    def prepare_resume(self, session_id: str, cwd: Path) -> PreparedSession:
        if not self.validate_session_id(session_id):
            raise ResumeFailed("Codex thread ID 无效")
        canonical = cwd.resolve(strict=True)
        try:
            result = self._request(
                op="resume",
                session_id=session_id,
                cwd=str(canonical),
            )
        except Exception as exc:
            raise ResumeFailed("Codex 无法恢复指定 thread") from exc
        if result.get("session_id") != session_id:
            raise ResumeFailed("Codex 恢复了错误的 thread")
        if Path(str(result.get("cwd", ""))).resolve(strict=True) != canonical:
            raise ResumeFailed("Codex thread 的 cwd 与目标目录不一致")
        return PreparedSession(self.name, session_id, canonical)

    def run_turn(
        self,
        prepared: PreparedSession,
        body: str,
        *,
        idempotency_key: str,
    ) -> TurnReceipt:
        result = self._request(
            op="turn",
            session_id=prepared.session_id,
            cwd=str(prepared.cwd),
            body=body,
            idempotency_key=idempotency_key,
        )
        status = TurnStatus(str(result.get("status")))
        reply = result.get("reply")
        return TurnReceipt(status, str(reply) if isinstance(reply, str) else None)
