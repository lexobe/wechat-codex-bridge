import json
import os

import pytest

from wechat_codex_bridge import (
    Binding,
    BindingConfigStore,
    BindingState,
    MockSessionProvider,
    SessionPathResolver,
)
from wechat_codex_bridge.session_models import PathSecurityError
from wechat_codex_bridge.session_paths import CONFIG_NAME


def validators(provider):
    return {provider.name: provider.validate_session_id}


def test_any_depth_existing_directory_and_safe_create(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    existing = root / "客户" / "项目甲" / "资料"
    existing.mkdir(parents=True)
    resolver = SessionPathResolver(root)

    assert resolver.resolve_existing("客户/项目甲/资料") == existing.resolve()

    create_target = resolver.resolve_for_create("客户/项目乙/子任务")
    created = resolver.create_directories(create_target)
    assert created.canonical_target == (root / "客户/项目乙/子任务").resolve()
    assert all(path.is_dir() for path in created.created)


@pytest.mark.parametrize("raw", [".", "..", "../逃逸", "/tmp", "客户/../../逃逸"])
def test_root_absolute_and_escape_paths_are_rejected(tmp_path, raw):
    root = tmp_path / "root"
    root.mkdir()
    resolver = SessionPathResolver(root)

    with pytest.raises(PathSecurityError):
        resolver.resolve_existing(raw)


def test_nonexistent_and_non_directory_targets_are_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "文件").write_text("x", encoding="utf-8")
    resolver = SessionPathResolver(root)

    with pytest.raises(PathSecurityError, match="不存在"):
        resolver.resolve_existing("未创建")
    with pytest.raises(PathSecurityError, match="不是目录"):
        resolver.resolve_existing("文件")


def test_symlink_escape_is_rejected(tmp_path):
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    os.symlink(outside, root / "逃逸")
    resolver = SessionPathResolver(root)

    with pytest.raises(PathSecurityError, match="逃出 ROOT"):
        resolver.resolve_existing("逃逸")
    with pytest.raises(PathSecurityError, match="逃出 ROOT"):
        resolver.resolve_for_create("逃逸/新目录")


def test_binding_store_distinguishes_absent_valid_and_invalid(tmp_path):
    directory = tmp_path / "项目"
    directory.mkdir()
    provider = MockSessionProvider()
    session_id = provider.create_session(directory, body=None, idempotency_key="seed")
    store = BindingConfigStore()

    assert store.read(directory, validators(provider)).state is BindingState.ABSENT

    store.write_atomic(
        directory,
        Binding(1, provider.name, session_id),
        validators(provider),
    )
    valid = store.read(directory, validators(provider))
    assert valid.state is BindingState.VALID
    assert valid.binding == Binding(1, provider.name, session_id)

    (directory / CONFIG_NAME).write_text("{", encoding="utf-8")
    assert store.read(directory, validators(provider)).state is BindingState.INVALID


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": 1, "provider": "unknown", "session_id": "x"},
        {
            "schema_version": 1,
            "provider": "mock",
            "session_id": "mock-session-1",
            "extra": True,
        },
        {"schema_version": 2, "provider": "mock", "session_id": "mock-session-1"},
        {"schema_version": 1, "provider": "mock", "session_id": ""},
    ],
)
def test_binding_schema_is_strict(tmp_path, payload):
    directory = tmp_path / "项目"
    directory.mkdir()
    (directory / CONFIG_NAME).write_text(json.dumps(payload), encoding="utf-8")
    provider = MockSessionProvider()

    result = BindingConfigStore().read(directory, validators(provider))
    assert result.state is BindingState.INVALID


def test_binding_symlink_is_invalid(tmp_path):
    directory = tmp_path / "项目"
    directory.mkdir()
    target = tmp_path / "outside.json"
    target.write_text("{}", encoding="utf-8")
    os.symlink(target, directory / CONFIG_NAME)

    result = BindingConfigStore().read(
        directory,
        validators(MockSessionProvider()),
    )
    assert result.state is BindingState.INVALID
