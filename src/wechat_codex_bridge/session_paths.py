from __future__ import annotations

import json
import os
import re
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Callable

from .session_models import (
    Binding,
    BindingRead,
    BindingState,
    PathSecurityError,
)
from .session_protocol import validate_route_path_token

CONFIG_NAME = ".session-chat.json"
MAX_CONFIG_BYTES = 16 * 1024
_PROVIDER_PATTERN = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


def _is_within(path: Path, root: Path, *, strict_child: bool = False) -> bool:
    try:
        relative = path.relative_to(root)
    except ValueError:
        return False
    return not strict_child or relative != Path(".")


@dataclass(frozen=True, slots=True)
class CreateTarget:
    lexical_target: Path
    relative_parts: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CreatedDirectory:
    canonical_target: Path
    created: tuple[Path, ...]


class SessionPathResolver:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise PathSecurityError("ROOT 必须是已存在目录")

    def _normalized_relative(self, raw_path: str) -> Path:
        validate_route_path_token(raw_path)
        windows = PureWindowsPath(raw_path)
        if (
            Path(raw_path).is_absolute()
            or windows.is_absolute()
            or windows.drive
            or windows.root
        ):
            raise PathSecurityError("拒绝绝对路径")

        normalized_text = os.path.normpath(raw_path)
        normalized = Path(normalized_text)
        if normalized == Path("."):
            raise PathSecurityError("ROOT 本身不是会话对象")
        candidate = Path(os.path.abspath(self.root / normalized))
        if not _is_within(candidate, self.root, strict_child=True):
            raise PathSecurityError("目录路径越出 ROOT")
        return candidate.relative_to(self.root)

    def lexical_target(self, raw_path: str) -> Path:
        return self.root / self._normalized_relative(raw_path)

    def resolve_existing(self, raw_path: str) -> Path:
        lexical_target = self.lexical_target(raw_path)
        try:
            canonical = lexical_target.resolve(strict=True)
        except FileNotFoundError as exc:
            raise PathSecurityError("目标目录不存在") from exc
        if not canonical.is_dir():
            raise PathSecurityError("目标不是目录")
        if not _is_within(canonical, self.root, strict_child=True):
            raise PathSecurityError("目标目录通过符号链接逃出 ROOT")
        return canonical

    def resolve_for_create(self, raw_path: str) -> CreateTarget:
        lexical_target = self.lexical_target(raw_path)
        relative = lexical_target.relative_to(self.root)
        if os.path.lexists(lexical_target):
            raise PathSecurityError("目标路径已经存在")

        current = self.root
        remaining: list[str] = []
        missing = False
        for part in relative.parts:
            candidate = current / part
            if not missing and os.path.lexists(candidate):
                try:
                    resolved = candidate.resolve(strict=True)
                except FileNotFoundError as exc:
                    raise PathSecurityError("路径包含断裂符号链接") from exc
                if not resolved.is_dir():
                    raise PathSecurityError("已存在的父路径不是目录")
                if not _is_within(resolved, self.root):
                    raise PathSecurityError("父路径通过符号链接逃出 ROOT")
                current = resolved
                continue
            missing = True
            remaining.append(part)

        target = current.joinpath(*remaining)
        if not _is_within(target, self.root, strict_child=True):
            raise PathSecurityError("待创建目录越出 ROOT")
        return CreateTarget(
            lexical_target=target,
            relative_parts=tuple(remaining),
        )

    def create_directories(self, target: CreateTarget) -> CreatedDirectory:
        if os.path.lexists(target.lexical_target):
            raise PathSecurityError("目标路径已经存在")

        current = target.lexical_target
        missing: list[Path] = []
        while not os.path.lexists(current):
            missing.append(current)
            current = current.parent
        try:
            canonical_parent = current.resolve(strict=True)
        except FileNotFoundError as exc:
            raise PathSecurityError("最近已存在父目录无效") from exc
        if not canonical_parent.is_dir() or not _is_within(canonical_parent, self.root):
            raise PathSecurityError("最近已存在父目录不安全")

        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        parent_fd = os.open(canonical_parent, flags)
        created: list[Path] = []
        parent = canonical_parent
        try:
            for unresolved in reversed(missing):
                name = unresolved.name
                try:
                    os.mkdir(name, mode=0o700, dir_fd=parent_fd)
                except FileExistsError as exc:
                    raise PathSecurityError("目录创建期间目标被并发占用") from exc
                child_fd = os.open(name, flags, dir_fd=parent_fd)
                try:
                    candidate = parent / name
                    canonical = candidate.resolve(strict=True)
                    safe = canonical.is_dir() and _is_within(
                        canonical, self.root, strict_child=True
                    )
                except Exception:
                    os.close(child_fd)
                    raise
                if not safe:
                    os.close(child_fd)
                    raise PathSecurityError("新建目录未处于 ROOT 内")
                created.append(canonical)
                os.close(parent_fd)
                parent_fd = child_fd
                parent = canonical
        finally:
            os.close(parent_fd)
        return CreatedDirectory(parent, tuple(created))


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"JSON 包含重复字段：{key}")
        result[key] = value
    return result


class BindingConfigStore:
    def __init__(self, max_bytes: int = MAX_CONFIG_BYTES) -> None:
        self.max_bytes = max_bytes

    @staticmethod
    def _validate_data(
        data: object,
        providers: dict[str, Callable[[str], bool]],
    ) -> Binding:
        if not isinstance(data, dict):
            raise ValueError("配置顶层必须是对象")
        if set(data) != {"schema_version", "provider", "session_id"}:
            raise ValueError("配置字段必须严格为三项")
        version = data["schema_version"]
        if not isinstance(version, int) or isinstance(version, bool) or version != 1:
            raise ValueError("不支持的 schema_version")
        provider = data["provider"]
        session_id = data["session_id"]
        if not isinstance(provider, str) or not _PROVIDER_PATTERN.fullmatch(provider):
            raise ValueError("provider 格式无效")
        if provider not in providers:
            raise ValueError("provider 未注册或未通过准入")
        if not isinstance(session_id, str) or not session_id or len(session_id) > 4096:
            raise ValueError("session_id 格式无效")
        if not providers[provider](session_id):
            raise ValueError("session_id 未通过 provider 校验")
        return Binding(1, provider, session_id)

    def read(
        self,
        directory: Path,
        providers: dict[str, Callable[[str], bool]],
    ) -> BindingRead:
        flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            directory_fd = os.open(directory, flags)
        except OSError as exc:
            return BindingRead(BindingState.INVALID, reason=f"目录状态不可确认：{exc}")
        try:
            metadata = os.stat(
                CONFIG_NAME,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            os.close(directory_fd)
            return BindingRead(BindingState.ABSENT)
        except OSError as exc:
            os.close(directory_fd)
            return BindingRead(BindingState.INVALID, reason=f"配置状态不可确认：{exc}")

        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            os.close(directory_fd)
            return BindingRead(BindingState.INVALID, reason="配置不是普通文件")
        if metadata.st_size > self.max_bytes:
            os.close(directory_fd)
            return BindingRead(BindingState.INVALID, reason="配置文件过大")

        file_flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            file_flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(CONFIG_NAME, file_flags, dir_fd=directory_fd)
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                data = json.load(stream, object_pairs_hook=_reject_duplicate_keys)
            binding = self._validate_data(data, providers)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            return BindingRead(BindingState.INVALID, reason=str(exc))
        finally:
            os.close(directory_fd)
        return BindingRead(BindingState.VALID, binding=binding)

    def write_atomic(
        self,
        directory: Path,
        binding: Binding,
        providers: dict[str, Callable[[str], bool]],
    ) -> None:
        data = {
            "schema_version": binding.schema_version,
            "provider": binding.provider,
            "session_id": binding.session_id,
        }
        self._validate_data(data, providers)
        encoded = (
            json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
        ).encode("utf-8")
        if len(encoded) > self.max_bytes:
            raise ValueError("配置内容过大")

        directory_flags = os.O_RDONLY
        if hasattr(os, "O_DIRECTORY"):
            directory_flags |= os.O_DIRECTORY
        if hasattr(os, "O_NOFOLLOW"):
            directory_flags |= os.O_NOFOLLOW
        directory_fd = os.open(directory, directory_flags)
        try:
            metadata = os.stat(
                CONFIG_NAME,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            metadata = None
        if metadata is not None and (
            stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode)
        ):
            os.close(directory_fd)
            raise ValueError("拒绝覆盖非普通配置文件")

        temporary_name = f".{CONFIG_NAME}.{secrets.token_hex(12)}.tmp"
        try:
            descriptor = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                0o600,
                dir_fd=directory_fd,
            )
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            read_descriptor = os.open(
                temporary_name,
                os.O_RDONLY,
                dir_fd=directory_fd,
            )
            with os.fdopen(read_descriptor, "r", encoding="utf-8") as stream:
                parsed = json.load(stream, object_pairs_hook=_reject_duplicate_keys)
            self._validate_data(parsed, providers)
            os.replace(
                temporary_name,
                CONFIG_NAME,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
            os.fsync(directory_fd)
        except Exception:
            if "descriptor" in locals():
                try:
                    os.unlink(temporary_name, dir_fd=directory_fd)
                except FileNotFoundError:
                    pass
            raise
        finally:
            os.close(directory_fd)
