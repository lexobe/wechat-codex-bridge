from __future__ import annotations

from .session_models import MessageKind, ParsedMessage, ProtocolError


def validate_route_path_token(raw_path: str) -> str:
    if not raw_path:
        raise ProtocolError("目录路径不能为空")
    if " " in raw_path:
        raise ProtocolError("目录路径不能包含 ASCII 空格")
    if "|" in raw_path or any(ord(char) < 32 or ord(char) == 127 for char in raw_path):
        raise ProtocolError("目录路径包含协议禁止字符")
    return raw_path


def parse_session_message(raw: str) -> ParsedMessage:
    """严格解析 `@路径 正文`、`@路径|new`、`@路径|create`。"""

    if not isinstance(raw, str) or not raw:
        raise ProtocolError("消息不能为空")
    if raw[0] != "@":
        raise ProtocolError("路由前缀 @ 必须位于字符位置 0")

    first_space = raw.find(" ")
    if first_space == -1:
        for command, kind in (
            ("new", MessageKind.NEW),
            ("create", MessageKind.CREATE),
        ):
            suffix = f"|{command}"
            if raw.endswith(suffix):
                raw_path = raw[1 : -len(suffix)]
                validate_route_path_token(raw_path)
                return ParsedMessage(kind=kind, raw_path=raw_path)
        raise ProtocolError("无空格消息必须精确匹配 |new 或 |create")

    raw_path = raw[1:first_space]
    body = raw[first_space + 1 :]
    validate_route_path_token(raw_path)
    if not body:
        raise ProtocolError("普通消息正文不能为空")
    return ParsedMessage(kind=MessageKind.ORDINARY, raw_path=raw_path, body=body)
