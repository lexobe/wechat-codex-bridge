from __future__ import annotations

import argparse
import json
import logging
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO
from uuid import uuid4

from .codex_provider import CodexAppServerProvider
from .session_models import SenderKey, SessionChatError, SessionInbound, TurnStatus
from .session_router import SessionChatRouter
from .store import Store

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RuntimeProbe:
    provider_mode: str
    provider_admitted: bool
    skill_discoverable: bool
    same_chrome_read_access: bool
    confirmed_x_writes: bool

    @property
    def ready(self) -> bool:
        return self.provider_admitted and self.skill_discoverable

    @property
    def x_ready(self) -> bool:
        return all(
            (self.skill_discoverable, self.same_chrome_read_access, self.confirmed_x_writes)
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "provider_mode": self.provider_mode,
            "provider_admitted": self.provider_admitted,
            "skill_discoverable": self.skill_discoverable,
            "same_chrome_read_access": self.same_chrome_read_access,
            "confirmed_x_writes": self.confirmed_x_writes,
            "ready": self.ready,
            "x_ready": self.x_ready,
        }


def probe_runtime(
    provider_mode: str,
    required_skill: str | Path,
    root: str | Path | None = None,
    provider: CodexAppServerProvider | None = None,
) -> RuntimeProbe:
    skill = Path(required_skill).resolve(strict=False)
    root_path = Path(root).resolve(strict=False) if root is not None else None
    try:
        relative = skill.relative_to(root_path) if root_path is not None else None
    except ValueError:
        relative = None
    skill_discoverable = (
        skill.is_file()
        and skill.name == "SKILL.md"
        and (
            root_path is None
            or (
                relative is not None
                and bool(relative.parts)
                and relative.parts[0] == "skills"
            )
        )
    )
    provider_admitted = False
    if provider_mode == "codex" and provider is not None and skill_discoverable:
        provider_probe = provider.probe()
        provider_admitted = provider_probe["provider_admitted"]
        same_chrome_read_access = provider_probe["same_chrome_read_access"]
    else:
        same_chrome_read_access = False
    return RuntimeProbe(
        provider_mode=provider_mode,
        provider_admitted=provider_admitted,
        skill_discoverable=skill_discoverable,
        same_chrome_read_access=same_chrome_read_access,
        confirmed_x_writes=False,
    )


def run_contract_probe(
    provider: CodexAppServerProvider,
    root: str | Path,
) -> dict[str, object]:
    """真实验证创建、跨进程恢复、cwd 与结构化 turn 状态。"""

    root_path = Path(root).resolve(strict=True)
    probe_directory = root_path / ".session-chat-contract-probe"
    if probe_directory.is_symlink():
        raise ValueError("合同探针目录不能是符号链接")
    probe_directory.mkdir(mode=0o700, exist_ok=True)
    canonical_probe_directory = probe_directory.resolve(strict=True)
    try:
        canonical_probe_directory.relative_to(root_path)
    except ValueError as exc:
        raise ValueError("合同探针目录越出 ROOT") from exc
    availability = provider.probe()
    if not availability["provider_admitted"]:
        raise ValueError("Codex 账号可用性探针未通过")

    key = f"contract-probe:{uuid4()}"
    session_id = provider.create_session(
        canonical_probe_directory,
        body=None,
        idempotency_key=f"{key}:create",
    )
    prepared = provider.prepare_resume(session_id, canonical_probe_directory)
    receipt = provider.run_turn(
        prepared,
        (
            "这是 session-chat 合同探针。不要调用工具、不要修改任何状态，"
            "只回复 SESSION_CHAT_CONTRACT_OK。"
        ),
        idempotency_key=f"{key}:turn",
    )
    if receipt.status is not TurnStatus.ACCEPTED_COMPLETED:
        raise ValueError(f"Codex 合同探针 turn 未完成：{receipt.status.value}")
    if receipt.reply is None or "SESSION_CHAT_CONTRACT_OK" not in receipt.reply:
        raise ValueError("Codex 合同探针没有返回约定回执")
    return {
        "passed": True,
        "provider": provider.name,
        "session_id": session_id,
        "root": str(root_path),
        "cwd": str(prepared.cwd),
        "turn_status": receipt.status.value,
        "same_chrome_read_access": availability["same_chrome_read_access"],
    }


class SessionRuntimeService:
    """供 OpenClaw 策略插件调用的持久进程服务；不注册 mock provider。"""

    def __init__(
        self,
        *,
        root: str | Path,
        db_path: str | Path,
        provider_mode: str,
        required_skill: str | Path,
        codex_runner: str | Path | None = None,
        codex_plugin_root: str | Path | None = None,
        openclaw_config: str | Path | None = None,
        openclaw_agent_dir: str | Path | None = None,
        chrome_codex_skill: str | Path | None = None,
        chrome_browser_client: str | Path | None = None,
        node_path: str = "node",
        model: str = "gpt-5.6-sol",
    ) -> None:
        provider = None
        if provider_mode == "codex":
            required = {
                "codex_runner": codex_runner,
                "codex_plugin_root": codex_plugin_root,
                "openclaw_config": openclaw_config,
                "openclaw_agent_dir": openclaw_agent_dir,
                "chrome_codex_skill": chrome_codex_skill,
                "chrome_browser_client": chrome_browser_client,
            }
            missing = [name for name, value in required.items() if value is None]
            if missing:
                raise ValueError(f"Codex provider 缺少配置：{', '.join(missing)}")
            provider = CodexAppServerProvider(
                runner=Path(str(codex_runner)),
                codex_plugin_root=Path(str(codex_plugin_root)),
                openclaw_config=Path(str(openclaw_config)),
                openclaw_agent_dir=Path(str(openclaw_agent_dir)),
                root=Path(root).resolve(strict=True),
                required_codex_skill=Path(required_skill),
                chrome_codex_skill=Path(str(chrome_codex_skill)),
                chrome_browser_client=Path(str(chrome_browser_client)),
                node_path=node_path,
                model=model,
            )
        self.probe = probe_runtime(provider_mode, required_skill, root, provider)
        if provider_mode == "codex" and not self.probe.ready:
            raise ValueError("真实 Codex provider 合同探针未通过")
        self.router = SessionChatRouter(
            root=root,
            store=Store(db_path),
            providers=[provider] if provider is not None else [],
            default_provider=provider.name if provider is not None else None,
            required_codex_skill=required_skill,
        )

    def dispatch(self, request: object) -> dict[str, object]:
        if not isinstance(request, dict):
            raise ValueError("请求必须是 JSON 对象")
        operation = request.get("op")
        if operation == "probe":
            return {"probe": self.probe.as_dict()}
        if operation != "route":
            raise ValueError("不支持的运行时操作")
        inbound = SessionInbound(
            sender=SenderKey(
                _required_string(request, "channel"),
                _required_string(request, "account_id"),
                _required_string(request, "sender_id"),
            ),
            message_id=_required_string(request, "message_id"),
            body=_required_body(request),
        )
        result = self.router.handle(inbound)
        return {
            "outcome": result.outcome.value,
            "turn_status": (
                result.turn_status.value if result.turn_status is not None else None
            ),
            "reply": result.reply,
            "duplicate": result.duplicate,
        }


def _required_string(request: dict[str, object], key: str) -> str:
    value = request.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} 必须是非空字符串")
    return value


def _required_body(request: dict[str, object]) -> str:
    value = request.get("body")
    if not isinstance(value, str):
        raise ValueError("body 必须是字符串")
    return value


def _safe_error(error: Exception) -> dict[str, str]:
    if isinstance(error, SessionChatError):
        return {
            "code": type(error).__name__,
            "message": str(error),
        }
    if isinstance(error, ValueError):
        return {
            "code": "InvalidRuntimeRequest",
            "message": str(error),
        }
    logger.exception("session-chat 运行时发生未分类错误")
    return {
        "code": "InternalFailClosed",
        "message": "目录会话运行时内部错误，已故障关闭",
    }


def serve(service: SessionRuntimeService, source: TextIO, sink: TextIO) -> None:
    for raw_line in source:
        request_id: object = None
        try:
            request = json.loads(raw_line)
            if isinstance(request, dict):
                request_id = request.get("id")
            result = service.dispatch(request)
            response = {"id": request_id, "ok": True, "result": result}
        except Exception as error:
            response = {
                "id": request_id,
                "ok": False,
                "error": _safe_error(error),
            }
        encoded = json.dumps(
            response,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        sink.write(encoded + "\n")
        sink.flush()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="session-chat 本地运行时")
    parser.add_argument("command", choices=("serve", "probe", "contract-probe"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--db", required=True)
    parser.add_argument(
        "--provider-mode",
        choices=("disabled", "codex"),
        default="disabled",
    )
    parser.add_argument("--required-codex-skill", required=True)
    parser.add_argument("--codex-runner")
    parser.add_argument("--codex-plugin-root")
    parser.add_argument("--openclaw-config")
    parser.add_argument("--openclaw-agent-dir")
    parser.add_argument("--chrome-codex-skill")
    parser.add_argument("--chrome-browser-client")
    parser.add_argument("--node-path", default="node")
    parser.add_argument("--model", default="gpt-5.6-sol")
    return parser


def _provider_from_arguments(
    arguments: argparse.Namespace,
) -> CodexAppServerProvider | None:
    if arguments.provider_mode != "codex":
        return None
    required = (
        arguments.codex_runner,
        arguments.codex_plugin_root,
        arguments.openclaw_config,
        arguments.openclaw_agent_dir,
        arguments.chrome_codex_skill,
        arguments.chrome_browser_client,
    )
    if not all(required):
        return None
    return CodexAppServerProvider(
        runner=Path(arguments.codex_runner),
        codex_plugin_root=Path(arguments.codex_plugin_root),
        openclaw_config=Path(arguments.openclaw_config),
        openclaw_agent_dir=Path(arguments.openclaw_agent_dir),
        root=Path(arguments.root).resolve(strict=True),
        required_codex_skill=Path(arguments.required_codex_skill),
        chrome_codex_skill=Path(arguments.chrome_codex_skill),
        chrome_browser_client=Path(arguments.chrome_browser_client),
        node_path=arguments.node_path,
        model=arguments.model,
    )


def main(argv: list[str] | None = None) -> int:
    arguments = build_parser().parse_args(argv)
    provider = _provider_from_arguments(arguments)
    if arguments.command == "probe":
        probe = probe_runtime(
            arguments.provider_mode,
            arguments.required_codex_skill,
            arguments.root,
            provider,
        )
        print(json.dumps(probe.as_dict(), ensure_ascii=False, sort_keys=True))
        return 0
    if arguments.command == "contract-probe":
        if provider is None:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "code": "ProviderNotAdmitted",
                            "message": "合同探针要求完整的 codex provider 配置",
                        },
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 2
        try:
            result = run_contract_probe(provider, arguments.root)
        except Exception as error:
            print(
                json.dumps({"ok": False, "error": _safe_error(error)}, ensure_ascii=False),
                file=sys.stderr,
            )
            return 2
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    try:
        service = SessionRuntimeService(
            root=arguments.root,
            db_path=arguments.db,
            provider_mode=arguments.provider_mode,
            required_skill=arguments.required_codex_skill,
            codex_runner=arguments.codex_runner,
            codex_plugin_root=arguments.codex_plugin_root,
            openclaw_config=arguments.openclaw_config,
            openclaw_agent_dir=arguments.openclaw_agent_dir,
            chrome_codex_skill=arguments.chrome_codex_skill,
            chrome_browser_client=arguments.chrome_browser_client,
            node_path=arguments.node_path,
            model=arguments.model,
        )
    except Exception as error:
        print(
            json.dumps({"ok": False, "error": _safe_error(error)}, ensure_ascii=False),
            file=sys.stderr,
        )
        return 2
    serve(service, sys.stdin, sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
