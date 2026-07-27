import pytest

from wechat_codex_bridge import (
    Binding,
    BindingConfigStore,
    MockSessionGateway,
    MockSessionProvider,
    RouteOutcome,
    SenderKey,
    SessionChatRouter,
    Store,
)
from wechat_codex_bridge.session_models import (
    BindingInvalid,
    ControlOperationBlocked,
    NoActiveSession,
    PathSecurityError,
    ProviderCapabilities,
    ProviderNotAdmitted,
    UnknownAfterProviderCreate,
)
from wechat_codex_bridge.session_paths import CONFIG_NAME

OWNER = SenderKey("wechat", "main", "owner")


def make_router(tmp_path, *, store=None, provider=None, config_store=None):
    root = tmp_path / "root"
    root.mkdir(exist_ok=True)
    provider = provider or MockSessionProvider()
    store = store or Store(tmp_path / "session.db")
    router = SessionChatRouter(
        root=root,
        store=store,
        providers=[provider],
        default_provider=provider.name,
        config_store=config_store,
    )
    gateway = MockSessionGateway(router, frozenset({OWNER}))
    return root, store, provider, router, gateway


def receive(gateway, message_id, body):
    return gateway.receive(
        channel=OWNER.channel,
        account_id=OWNER.account_id,
        sender_id=OWNER.sender_id,
        message_id=message_id,
        body=body,
    )


def seed_binding(directory, provider):
    session_id = provider.create_session(
        directory, body=None, idempotency_key=f"seed:{directory.name}"
    )
    BindingConfigStore().write_atomic(
        directory,
        Binding(1, provider.name, session_id),
        {provider.name: provider.validate_session_id},
    )
    return session_id


def test_bound_route_switch_and_unbound_fallback(tmp_path):
    root, _, provider, router, gateway = make_router(tmp_path)
    bound = root / "客户" / "项目甲"
    unbound = root / "客户" / "未绑定"
    bound.mkdir(parents=True)
    unbound.mkdir()
    session_id = seed_binding(bound, provider)

    first = receive(gateway, "m-1", "@客户/项目甲 第一条")
    fallback = receive(gateway, "m-2", "@客户/未绑定 a | b")

    assert first.outcome is RouteOutcome.DELIVERED
    assert fallback.outcome is RouteOutcome.FALLBACK_TO_ACTIVE
    assert fallback.session_id == session_id
    assert provider.sessions[session_id][1] == ["第一条", "a | b"]
    assert router.active_for(OWNER).session_id == session_id


def test_unbound_without_active_fails_closed(tmp_path):
    root, _, _, _, gateway = make_router(tmp_path)
    (root / "空目录").mkdir()

    with pytest.raises(NoActiveSession):
        receive(gateway, "m-1", "@空目录 正文")


def test_nonexistent_ordinary_never_falls_back_or_creates(tmp_path):
    root, _, provider, router, gateway = make_router(tmp_path)
    bound = root / "已有"
    bound.mkdir()
    session_id = seed_binding(bound, provider)
    receive(gateway, "m-1", "@已有 建立 active")

    with pytest.raises(PathSecurityError, match="不存在"):
        receive(gateway, "m-2", "@不存在/目录 不得回落")

    assert not (root / "不存在").exists()
    assert provider.sessions[session_id][1] == ["建立 active"]
    assert router.active_for(OWNER).session_id == session_id


def test_invalid_binding_and_resume_failure_never_fall_back(tmp_path):
    root, _, provider, router, gateway = make_router(tmp_path)
    good = root / "good"
    invalid = root / "invalid"
    missing_session = root / "missing-session"
    good.mkdir()
    invalid.mkdir()
    missing_session.mkdir()
    good_id = seed_binding(good, provider)
    receive(gateway, "m-1", "@good 建立 active")

    (invalid / CONFIG_NAME).write_text("{", encoding="utf-8")
    with pytest.raises(BindingInvalid):
        receive(gateway, "m-2", "@invalid 不得回落")

    BindingConfigStore().write_atomic(
        missing_session,
        Binding(1, provider.name, f"{provider.name}-session-999"),
        {provider.name: provider.validate_session_id},
    )
    with pytest.raises(Exception, match="无法恢复"):
        receive(gateway, "m-3", "@missing-session 不得回落")

    assert provider.sessions[good_id][1] == ["建立 active"]
    assert router.active_for(OWNER).session_id == good_id


def test_new_creates_first_binding_replaces_binding_and_is_idempotent(tmp_path):
    root, _, provider, router, gateway = make_router(tmp_path)
    directory = root / "项目"
    directory.mkdir()

    first = receive(gateway, "new-1", "@项目|new")
    duplicate = receive(gateway, "new-1", "@项目|new")
    second = receive(gateway, "new-2", "@项目|new")

    assert first.outcome is RouteOutcome.SESSION_CREATED
    assert duplicate.duplicate is True
    assert second.session_id != first.session_id
    assert len(provider.create_calls) == 2
    assert router.active_for(OWNER).session_id == second.session_id
    binding = (
        BindingConfigStore()
        .read(directory, {provider.name: provider.validate_session_id})
        .binding
    )
    assert binding.session_id == second.session_id


def test_create_builds_nested_directory_and_first_session(tmp_path):
    root, _, provider, router, gateway = make_router(tmp_path)

    created = receive(gateway, "create-1", "@客户/项目乙/子任务|create")
    duplicate = receive(gateway, "create-1", "@客户/项目乙/子任务|create")

    target = root / "客户/项目乙/子任务"
    assert target.is_dir()
    assert (target / CONFIG_NAME).is_file()
    assert created.outcome is RouteOutcome.DIRECTORY_CREATED
    assert duplicate.duplicate is True
    assert len(provider.create_calls) == 1
    assert router.active_for(OWNER).session_id == created.session_id


def test_create_existing_directory_fails_and_new_is_required(tmp_path):
    root, _, provider, router, gateway = make_router(tmp_path)
    existing = root / "已有无绑定"
    existing.mkdir()

    with pytest.raises(PathSecurityError, match="已经存在"):
        receive(gateway, "create-1", "@已有无绑定|create")

    result = receive(gateway, "new-1", "@已有无绑定|new")
    assert result.outcome is RouteOutcome.SESSION_CREATED
    assert len(provider.create_calls) == 1


def test_new_never_overwrites_invalid_binding(tmp_path):
    root, _, provider, router, gateway = make_router(tmp_path)
    directory = root / "损坏"
    directory.mkdir()
    config = directory / CONFIG_NAME
    config.write_text("{", encoding="utf-8")

    with pytest.raises(BindingInvalid):
        receive(gateway, "new-1", "@损坏|new")

    assert config.read_text(encoding="utf-8") == "{"
    assert provider.create_calls == []
    assert router.active_for(OWNER) is None


def test_create_provider_failure_keeps_visible_directories_and_old_active(tmp_path):
    root, _, provider, router, gateway = make_router(tmp_path)
    old = root / "old"
    old.mkdir()
    old_id = seed_binding(old, provider)
    receive(gateway, "m-1", "@old 建立 active")
    provider.fail_create = True

    with pytest.raises(RuntimeError, match="创建失败"):
        receive(gateway, "create-1", "@保留/空目录|create")

    assert (root / "保留/空目录").is_dir()
    assert not (root / "保留/空目录" / CONFIG_NAME).exists()
    assert router.active_for(OWNER).session_id == old_id


def test_restart_has_no_active_but_can_resume_binding(tmp_path):
    root, store, provider, _, gateway = make_router(tmp_path)
    result = receive(gateway, "create-1", "@项目|create")
    (root / "无绑定").mkdir()

    restarted = SessionChatRouter(
        root=root,
        store=store,
        providers=[provider],
        default_provider=provider.name,
    )
    restarted_gateway = MockSessionGateway(restarted, frozenset({OWNER}))
    assert restarted.active_for(OWNER) is None

    with pytest.raises(NoActiveSession):
        receive(restarted_gateway, "m-1", "@无绑定 正文")

    resumed = receive(restarted_gateway, "m-2", "@项目 重启后续接")
    assert resumed.session_id == result.session_id
    assert provider.sessions[result.session_id][1] == ["重启后续接"]


def test_ordinary_message_id_is_idempotent(tmp_path):
    root, _, provider, _, gateway = make_router(tmp_path)
    directory = root / "项目"
    directory.mkdir()
    session_id = seed_binding(directory, provider)

    first = receive(gateway, "m-1", "@项目 只执行一次")
    duplicate = receive(gateway, "m-1", "@项目 只执行一次")

    assert first.duplicate is False
    assert duplicate.duplicate is True
    assert provider.sessions[session_id][1] == ["只执行一次"]


def test_mock_gateway_rejects_unauthorized_sender(tmp_path):
    _, _, _, router, _ = make_router(tmp_path)
    gateway = MockSessionGateway(router, frozenset({OWNER}))

    with pytest.raises(PermissionError):
        gateway.receive(
            channel="wechat",
            account_id="main",
            sender_id="other",
            message_id="m-1",
            body="@项目 正文",
        )


class FailSessionCreatedStore(Store):
    def mark_session_created(self, **kwargs):
        raise OSError("模拟 SESSION_CREATED 持久化失败")


class FailUnknownMarkerStore(FailSessionCreatedStore):
    def update_session_control(self, **kwargs):
        if kwargs.get("new_state") == "unknown_after_provider_create":
            raise OSError("模拟 UNKNOWN 标记也失败")
        return super().update_session_control(**kwargs)


class FailBindingStore(BindingConfigStore):
    def write_atomic(self, *args, **kwargs):
        raise OSError("模拟配置提交失败")


def test_session_created_persistence_failure_marks_unknown_and_never_retries(tmp_path):
    store = FailSessionCreatedStore(tmp_path / "session.db")
    root, store, provider, router, gateway = make_router(tmp_path, store=store)
    (root / "项目").mkdir()

    with pytest.raises(UnknownAfterProviderCreate):
        receive(gateway, "new-1", "@项目|new")

    operation = store.get_session_control(
        channel="wechat",
        account_id="main",
        sender_id="owner",
        message_id="new-1",
    )
    assert operation["state"] == "unknown_after_provider_create"
    assert operation["provider_create_started_at"]
    assert operation["provider_create_returned_at"]
    assert operation["manual_review_required"] == 1
    assert (
        router.inspect_control_operation(OWNER, "new-1")["state"]
        == "unknown_after_provider_create"
    )
    assert not (root / "项目" / CONFIG_NAME).exists()
    assert router.active_for(OWNER) is None
    assert len(provider.create_calls) == 1

    with pytest.raises(ControlOperationBlocked):
        receive(gateway, "new-1", "@项目|new")
    assert len(provider.create_calls) == 1

    restarted = SessionChatRouter(
        root=root,
        store=Store(store.path),
        providers=[provider],
        default_provider=provider.name,
    )
    restarted_gateway = MockSessionGateway(restarted, frozenset({OWNER}))
    with pytest.raises(ControlOperationBlocked):
        receive(restarted_gateway, "new-1", "@项目|new")
    assert len(provider.create_calls) == 1


def test_create_session_created_persistence_failure_keeps_directory(tmp_path):
    store = FailSessionCreatedStore(tmp_path / "session.db")
    root, store, provider, router, gateway = make_router(tmp_path, store=store)

    with pytest.raises(UnknownAfterProviderCreate):
        receive(gateway, "create-1", "@保留/目标|create")

    assert (root / "保留/目标").is_dir()
    assert not (root / "保留/目标" / CONFIG_NAME).exists()
    assert router.active_for(OWNER) is None
    operation = store.get_session_control(
        channel="wechat",
        account_id="main",
        sender_id="owner",
        message_id="create-1",
    )
    assert operation["state"] == "unknown_after_provider_create"
    assert len(provider.create_calls) == 1


def test_unknown_marker_failure_keeps_preexisting_in_progress_anchor(tmp_path):
    store = FailUnknownMarkerStore(tmp_path / "session.db")
    root, store, provider, router, gateway = make_router(tmp_path, store=store)
    (root / "项目").mkdir()

    with pytest.raises(UnknownAfterProviderCreate):
        receive(gateway, "new-1", "@项目|new")

    operation = store.get_session_control(
        channel="wechat",
        account_id="main",
        sender_id="owner",
        message_id="new-1",
    )
    assert operation["state"] == "in_progress"
    assert operation["provider_create_started_at"]
    assert operation["manual_review_required"] == 0
    assert len(provider.create_calls) == 1
    with pytest.raises(ControlOperationBlocked):
        receive(gateway, "new-1", "@项目|new")
    assert len(provider.create_calls) == 1


def test_config_commit_failure_keeps_create_directory_and_records_unknown(tmp_path):
    root, store, provider, router, gateway = make_router(
        tmp_path, config_store=FailBindingStore()
    )

    with pytest.raises(UnknownAfterProviderCreate):
        receive(gateway, "create-1", "@保留/目标|create")

    target = root / "保留/目标"
    assert target.is_dir()
    assert not (target / CONFIG_NAME).exists()
    assert router.active_for(OWNER) is None
    operation = store.get_session_control(
        channel="wechat",
        account_id="main",
        sender_id="owner",
        message_id="create-1",
    )
    assert operation["state"] == "unknown_after_provider_create"
    assert operation["session_ref"].startswith("mock-session-")
    assert operation["created_directories_json"]
    assert operation["manual_review_required"] == 1


def test_new_config_failure_preserves_old_binding_and_old_active(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    store = Store(tmp_path / "session.db")
    provider = MockSessionProvider()
    directory = root / "项目"
    directory.mkdir()
    old_id = seed_binding(directory, provider)
    router = SessionChatRouter(
        root=root,
        store=store,
        providers=[provider],
        default_provider=provider.name,
        config_store=FailBindingStore(),
    )
    gateway = MockSessionGateway(router, frozenset({OWNER}))
    receive(gateway, "m-1", "@项目 建立旧 active")

    with pytest.raises(UnknownAfterProviderCreate):
        receive(gateway, "new-1", "@项目|new")

    binding = (
        BindingConfigStore()
        .read(
            directory,
            {provider.name: provider.validate_session_id},
        )
        .binding
    )
    assert binding.session_id == old_id
    assert router.active_for(OWNER).session_id == old_id
    assert provider.sessions[old_id][1] == ["建立旧 active"]


class RejectedProvider(MockSessionProvider):
    def capabilities(self):
        return ProviderCapabilities(
            create_without_body=False,
            stable_session_id=True,
            cross_process_resume=True,
            stable_cwd=True,
            structured_turn_status=True,
            long_lived_session=True,
        )


def test_provider_must_pass_all_admission_capabilities(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(ProviderNotAdmitted):
        SessionChatRouter(
            root=root,
            store=Store(tmp_path / "session.db"),
            providers=[RejectedProvider()],
            default_provider="mock",
        )


class CodexWithoutXProbe(MockSessionProvider):
    name = "codex"


class CodexWithSkillProbe(MockSessionProvider):
    name = "codex"

    def capabilities(self):
        return ProviderCapabilities(
            create_without_body=True,
            stable_session_id=True,
            cross_process_resume=True,
            stable_cwd=True,
            structured_turn_status=True,
            long_lived_session=True,
            workspace_skill_discovery=True,
            same_chrome_read_access=True,
            confirmed_x_writes=True,
        )


def test_codex_provider_requires_discoverable_x_skill_but_x_tools_are_separate(
    tmp_path,
):
    root = tmp_path / "root"
    root.mkdir()
    skill = root / "skills" / "x-twitter-chrome" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: x-twitter-chrome\n---\n", encoding="utf-8")

    with pytest.raises(ProviderNotAdmitted, match="技能发现"):
        SessionChatRouter(
            root=root,
            store=Store(tmp_path / "no-x.db"),
            providers=[CodexWithoutXProbe(name="codex")],
            default_provider="codex",
            required_codex_skill=skill,
        )

    admitted = SessionChatRouter(
        root=root,
        store=Store(tmp_path / "with-x.db"),
        providers=[CodexWithSkillProbe(name="codex")],
        default_provider="codex",
        required_codex_skill=skill,
    )
    assert "codex" in admitted.providers


def test_codex_provider_rejects_missing_skill_file(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(ProviderNotAdmitted, match="技能文件不可发现"):
        SessionChatRouter(
            root=root,
            store=Store(tmp_path / "missing-skill.db"),
            providers=[CodexWithSkillProbe(name="codex")],
            default_provider="codex",
            required_codex_skill=root / "skills" / "missing" / "SKILL.md",
        )


def test_codex_resume_rechecks_x_skill_and_fails_closed_if_removed(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    skill = root / "skills" / "x-twitter-chrome" / "SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: x-twitter-chrome\n---\n", encoding="utf-8")
    provider = CodexWithSkillProbe(name="codex")
    directory = root / "项目"
    directory.mkdir()
    session_id = seed_binding(directory, provider)
    router = SessionChatRouter(
        root=root,
        store=Store(tmp_path / "recheck.db"),
        providers=[provider],
        default_provider="codex",
        required_codex_skill=skill,
    )
    gateway = MockSessionGateway(router, frozenset({OWNER}))
    skill.unlink()

    with pytest.raises(ProviderNotAdmitted, match="技能文件不可发现"):
        receive(gateway, "m-1", "@项目 不得恢复")

    assert provider.sessions[session_id][1] == []
