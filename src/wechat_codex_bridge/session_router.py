from __future__ import annotations

import hashlib
import json
import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, TypedDict

from .session_models import (
    ActiveSession,
    Binding,
    BindingInvalid,
    BindingState,
    ControlOperationBlocked,
    DuplicateMessageBlocked,
    MessageKind,
    NoActiveSession,
    ProviderNotAdmitted,
    RouteOutcome,
    RouteResult,
    SenderKey,
    SessionInbound,
    TurnStatus,
    UnknownAfterProviderCreate,
)
from .session_paths import BindingConfigStore, SessionPathResolver
from .session_protocol import parse_session_message
from .session_provider import SessionProvider
from .store import Store

logger = logging.getLogger(__name__)


class _ControlKey(TypedDict):
    channel: str
    account_id: str
    sender_id: str
    message_id: str


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _result_to_dict(result: RouteResult) -> dict[str, object]:
    return {
        "outcome": result.outcome.value,
        "provider": result.provider,
        "session_id": result.session_id,
        "directory": result.directory,
        "turn_status": (
            result.turn_status.value if result.turn_status is not None else None
        ),
        "reply": result.reply,
    }


def _result_from_json(raw: object, *, duplicate: bool) -> RouteResult:
    if not isinstance(raw, str):
        raise ControlOperationBlocked("已完成操作缺少持久化结果")
    data = json.loads(raw)
    turn_status = data.get("turn_status")
    return RouteResult(
        outcome=RouteOutcome(data["outcome"]),
        provider=str(data["provider"]),
        session_id=str(data["session_id"]),
        directory=str(data["directory"]),
        turn_status=TurnStatus(turn_status) if turn_status else None,
        reply=data.get("reply"),
        duplicate=duplicate,
    )


class _LockRegistry:
    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._locks: dict[object, threading.RLock] = {}

    def get(self, key: object) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(key, threading.RLock())


class SessionChatRouter:
    """冻结协议的单进程安全路由器；只注册通过准入的 provider。"""

    def __init__(
        self,
        *,
        root: str | Path,
        store: Store,
        providers: list[SessionProvider],
        default_provider: str | None,
        required_codex_skill: str | Path | None = None,
        config_store: BindingConfigStore | None = None,
    ) -> None:
        self.paths = SessionPathResolver(root)
        self.store = store
        self.config_store = config_store or BindingConfigStore()
        self.required_codex_skill = (
            Path(required_codex_skill).resolve(strict=False)
            if required_codex_skill is not None
            else None
        )
        self.providers: dict[str, SessionProvider] = {}
        for provider in providers:
            capabilities = provider.capabilities()
            if not capabilities.mvp_ready:
                raise ProviderNotAdmitted(f"provider {provider.name} 未通过 MVP 准入")
            if provider.name == "codex":
                self._assert_codex_requirements(provider)
            if provider.name in self.providers:
                raise ValueError(f"provider 重复注册：{provider.name}")
            self.providers[provider.name] = provider
        if default_provider is not None and default_provider not in self.providers:
            raise ProviderNotAdmitted("default_provider 未通过准入或未注册")
        self.default_provider = default_provider
        self._active: dict[SenderKey, ActiveSession] = {}
        self._namespace_lock = threading.RLock()
        self._sender_locks = _LockRegistry()
        self._directory_locks = _LockRegistry()
        self._session_locks = _LockRegistry()

    @property
    def root(self) -> Path:
        return self.paths.root

    def active_for(self, sender: SenderKey) -> ActiveSession | None:
        return self._active.get(sender)

    def inspect_control_operation(
        self, sender: SenderKey, message_id: str
    ) -> dict[str, object] | None:
        """供本机管理员只读核对未知事务；不会自动恢复或清理。"""

        return self.store.get_session_control(
            channel=sender.channel,
            account_id=sender.account_id,
            sender_id=sender.sender_id,
            message_id=message_id,
        )

    def _validators(self) -> dict[str, Callable[[str], bool]]:
        return {
            name: provider.validate_session_id
            for name, provider in self.providers.items()
        }

    @staticmethod
    def _control_key(inbound: SessionInbound) -> _ControlKey:
        return {
            "channel": inbound.sender.channel,
            "account_id": inbound.sender.account_id,
            "sender_id": inbound.sender.sender_id,
            "message_id": inbound.message_id,
        }

    def handle(self, inbound: SessionInbound) -> RouteResult:
        parsed = parse_session_message(inbound.body)
        sender_lock = self._sender_locks.get(inbound.sender)
        with sender_lock, self._namespace_lock:
            if parsed.kind is MessageKind.ORDINARY:
                assert parsed.body is not None
                return self._handle_ordinary(inbound, parsed.raw_path, parsed.body)
            if parsed.kind is MessageKind.NEW:
                return self._handle_new(inbound, parsed.raw_path)
            return self._handle_create(inbound, parsed.raw_path)

    def _provider(self, name: str) -> SessionProvider:
        try:
            provider = self.providers[name]
        except KeyError as exc:
            raise ProviderNotAdmitted(f"provider 未注册：{name}") from exc
        if name == "codex":
            self._assert_codex_requirements(provider)
        return provider

    def _assert_codex_requirements(self, provider: SessionProvider) -> None:
        if self.required_codex_skill is None:
            raise ProviderNotAdmitted("Codex provider 缺少必需的 X 技能路径")
        try:
            skill_relative = self.required_codex_skill.relative_to(self.root)
        except ValueError as exc:
            raise ProviderNotAdmitted(
                "Codex provider 的 X 技能必须位于 ROOT 内"
            ) from exc
        if (
            not self.required_codex_skill.is_file()
            or self.required_codex_skill.name != "SKILL.md"
            or not skill_relative.parts
            or skill_relative.parts[0] != "skills"
        ):
            raise ProviderNotAdmitted("Codex provider 的 X 技能文件不可发现")
        if not provider.capabilities().workspace_skill_discovery:
            raise ProviderNotAdmitted(
                "Codex provider 未通过工作区 X 技能发现准入"
            )

    def _require_default_provider(self) -> str:
        if self.default_provider is None:
            raise ProviderNotAdmitted("没有通过准入的 default_provider")
        return self.default_provider

    def _read_binding(self, directory: Path):
        return self.config_store.read(directory, self._validators())

    def _handle_ordinary(
        self, inbound: SessionInbound, raw_path: str, body: str
    ) -> RouteResult:
        directory = self.paths.resolve_existing(raw_path)
        directory_lock = self._directory_locks.get(directory)
        with directory_lock:
            binding_read = self._read_binding(directory)
            active = self._active.get(inbound.sender)
            if binding_read.state is BindingState.INVALID:
                raise BindingInvalid(binding_read.reason or "目录绑定无效")
            if binding_read.state is BindingState.ABSENT:
                if active is None:
                    raise NoActiveSession("目录无绑定且发送者没有旧 active")
                target = active
                outcome = RouteOutcome.FALLBACK_TO_ACTIVE
            else:
                assert binding_read.binding is not None
                binding = binding_read.binding
                target = ActiveSession(
                    directory=directory,
                    provider=binding.provider,
                    session_id=binding.session_id,
                )
                outcome = RouteOutcome.DELIVERED

        provider = self._provider(target.provider)
        message_hash = _sha256(inbound.body)
        created, record = self.store.begin_session_route(
            **self._control_key(inbound),
            message_hash=message_hash,
        )
        if not created:
            if record["state"] == "completed":
                return _result_from_json(record["result_json"], duplicate=True)
            raise DuplicateMessageBlocked(
                "相同 message_id 的普通消息仍在处理中或结果未知"
            )

        try:
            prepared = provider.prepare_resume(target.session_id, target.directory)
            if outcome is RouteOutcome.DELIVERED:
                self._active[inbound.sender] = target
        except Exception:
            self.store.abandon_session_route(**self._control_key(inbound))
            if outcome is RouteOutcome.DELIVERED and active is not None:
                self._active[inbound.sender] = active
            elif outcome is RouteOutcome.DELIVERED:
                self._active.pop(inbound.sender, None)
            raise

        session_lock = self._session_locks.get((target.provider, target.session_id))
        try:
            with session_lock:
                receipt = provider.run_turn(
                    prepared,
                    body,
                    idempotency_key=inbound.message_id,
                )
            result = RouteResult(
                outcome=outcome,
                provider=target.provider,
                session_id=target.session_id,
                directory=str(target.directory),
                turn_status=receipt.status,
                reply=receipt.reply,
            )
            self.store.finish_session_route(
                **self._control_key(inbound),
                result=_result_to_dict(result),
            )
            return result
        except Exception:
            self.store.mark_session_route_unknown(**self._control_key(inbound))
            raise

    def _claim_control(
        self,
        inbound: SessionInbound,
        *,
        command: MessageKind,
        target_key: str,
    ) -> RouteResult | None:
        created, record = self.store.begin_session_control(
            **self._control_key(inbound),
            command=command.value,
            target_key=target_key,
            message_hash=_sha256(inbound.body),
        )
        if created:
            return None
        if record["state"] == "completed":
            return _result_from_json(record["result_json"], duplicate=True)
        raise ControlOperationBlocked(
            f"控制请求处于 {record['state']}，禁止自动重复创建"
        )

    def _mark_control_failed(
        self, inbound: SessionInbound, *, expected_states: tuple[str, ...], error: str
    ) -> None:
        try:
            self.store.update_session_control(
                **self._control_key(inbound),
                expected_states=expected_states,
                new_state="failed",
                error=error,
            )
        except Exception:
            logger.exception("控制操作失败状态无法持久化，保留原幂等状态")

    def _mark_unknown_after_create(
        self,
        inbound: SessionInbound,
        *,
        provider: str,
        session_id: str,
        error: Exception,
    ) -> None:
        try:
            self.store.update_session_control(
                **self._control_key(inbound),
                expected_states=(
                    "in_progress",
                    "directory_created",
                    "session_created",
                    "binding_committed",
                ),
                new_state="unknown_after_provider_create",
                provider=provider,
                session_ref=session_id,
                error=str(error),
            )
        except Exception:
            logger.exception(
                "UNKNOWN_AFTER_PROVIDER_CREATE 无法持久化；"
                "保留预先写入的事务状态并要求人工核对"
            )

    def _create_and_bind(
        self,
        inbound: SessionInbound,
        *,
        directory: Path,
        provider_name: str,
        outcome: RouteOutcome,
    ) -> RouteResult:
        try:
            provider = self._provider(provider_name)
        except Exception as exc:
            self._mark_control_failed(
                inbound,
                expected_states=("in_progress", "directory_created"),
                error=str(exc),
            )
            raise
        key = f"{inbound.sender.channel}:{inbound.sender.account_id}:"
        key += f"{inbound.sender.sender_id}:{inbound.message_id}"
        try:
            self.store.mark_provider_create_started(
                **self._control_key(inbound),
                provider=provider_name,
            )
            session_id = provider.create_session(
                directory,
                body=None,
                idempotency_key=key,
            )
        except Exception as exc:
            self._mark_control_failed(
                inbound,
                expected_states=("in_progress", "directory_created"),
                error=str(exc),
            )
            raise

        try:
            self.store.mark_session_created(
                **self._control_key(inbound),
                provider=provider_name,
                session_ref=session_id,
            )
        except Exception as exc:
            self._mark_unknown_after_create(
                inbound,
                provider=provider_name,
                session_id=session_id,
                error=exc,
            )
            raise UnknownAfterProviderCreate(
                "provider 已创建 session，但 SESSION_CREATED 无法可靠持久化"
            ) from exc

        binding = Binding(1, provider_name, session_id)
        try:
            self.config_store.write_atomic(
                directory,
                binding,
                self._validators(),
            )
            self.store.update_session_control(
                **self._control_key(inbound),
                expected_states=("session_created",),
                new_state="binding_committed",
            )
            result = RouteResult(
                outcome=outcome,
                provider=provider_name,
                session_id=session_id,
                directory=str(directory),
            )
            self.store.update_session_control(
                **self._control_key(inbound),
                expected_states=("binding_committed",),
                new_state="completed",
                result=_result_to_dict(result),
            )
        except Exception as exc:
            self._mark_unknown_after_create(
                inbound,
                provider=provider_name,
                session_id=session_id,
                error=exc,
            )
            raise UnknownAfterProviderCreate(
                "provider 已创建 session，但绑定事务无法可靠确认"
            ) from exc

        self._active[inbound.sender] = ActiveSession(
            directory=directory,
            provider=provider_name,
            session_id=session_id,
        )
        return result

    def _handle_new(self, inbound: SessionInbound, raw_path: str) -> RouteResult:
        directory = self.paths.resolve_existing(raw_path)
        duplicate = self._claim_control(
            inbound,
            command=MessageKind.NEW,
            target_key=str(directory),
        )
        if duplicate is not None:
            return duplicate

        with self._directory_locks.get(directory):
            binding_read = self._read_binding(directory)
            if binding_read.state is BindingState.INVALID:
                self._mark_control_failed(
                    inbound,
                    expected_states=("in_progress",),
                    error=binding_read.reason or "绑定无效",
                )
                raise BindingInvalid(binding_read.reason or "绑定无效")
            if binding_read.state is BindingState.ABSENT:
                try:
                    provider_name = self._require_default_provider()
                except ProviderNotAdmitted as exc:
                    self._mark_control_failed(
                        inbound,
                        expected_states=("in_progress",),
                        error=str(exc),
                    )
                    raise
            else:
                assert binding_read.binding is not None
                provider_name = binding_read.binding.provider
            return self._create_and_bind(
                inbound,
                directory=directory,
                provider_name=provider_name,
                outcome=RouteOutcome.SESSION_CREATED,
            )

    def _handle_create(self, inbound: SessionInbound, raw_path: str) -> RouteResult:
        lexical_target = self.paths.lexical_target(raw_path)
        duplicate = self._claim_control(
            inbound,
            command=MessageKind.CREATE,
            target_key=str(lexical_target),
        )
        if duplicate is not None:
            return duplicate

        try:
            provider_name = self._require_default_provider()
        except ProviderNotAdmitted as exc:
            self._mark_control_failed(
                inbound,
                expected_states=("in_progress",),
                error=str(exc),
            )
            raise

        target_lock = self._directory_locks.get(lexical_target)
        with target_lock:
            try:
                target = self.paths.resolve_for_create(raw_path)
                created = self.paths.create_directories(target)
                self.store.mark_session_directories_created(
                    **self._control_key(inbound),
                    directories=[str(path) for path in created.created],
                )
            except Exception as exc:
                self._mark_control_failed(
                    inbound,
                    expected_states=("in_progress",),
                    error=str(exc),
                )
                raise
            return self._create_and_bind(
                inbound,
                directory=created.canonical_target,
                provider_name=provider_name,
                outcome=RouteOutcome.DIRECTORY_CREATED,
            )


@dataclass
class MockSessionGateway:
    """授权后把纯文本交给路由器；不会连接真实微信或外部服务。"""

    router: SessionChatRouter
    authorized_senders: frozenset[SenderKey]

    def receive(
        self,
        *,
        channel: str,
        account_id: str,
        sender_id: str,
        message_id: str,
        body: str,
    ) -> RouteResult:
        sender = SenderKey(channel, account_id, sender_id)
        if sender not in self.authorized_senders:
            raise PermissionError("发送者未获授权")
        return self.router.handle(SessionInbound(sender, message_id, body))
