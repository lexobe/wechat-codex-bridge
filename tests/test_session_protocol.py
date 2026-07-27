import pytest

from wechat_codex_bridge import MessageKind, parse_session_message
from wechat_codex_bridge.session_models import ProtocolError


@pytest.mark.parametrize(
    "raw",
    [
        " @客户/项目甲 正文",
        "\t@客户/项目甲 正文",
        "\n@客户/项目甲 正文",
        "说明：@客户/项目甲 正文",
        "\ufeff@客户/项目甲 正文",
    ],
)
def test_route_prefix_must_be_at_position_zero(raw):
    with pytest.raises(ProtocolError, match="位置 0"):
        parse_session_message(raw)


def test_ordinary_body_is_preserved_without_control_parsing():
    parsed = parse_session_message(
        "@客户/项目甲 请解释 a | b、|create、/new\n以及 Markdown | 表格"
    )

    assert parsed.kind is MessageKind.ORDINARY
    assert parsed.raw_path == "客户/项目甲"
    assert parsed.body == "请解释 a | b、|create、/new\n以及 Markdown | 表格"


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("@客户/项目甲|new", MessageKind.NEW),
        ("@客户/新项目|create", MessageKind.CREATE),
    ],
)
def test_control_commands_must_match_exactly(raw, kind):
    parsed = parse_session_message(raw)
    assert parsed.kind is kind
    assert parsed.body is None


@pytest.mark.parametrize(
    "raw",
    [
        "@客户/项目甲|NEW",
        "@客户/项目甲|CREATE",
        "@客户/项目甲|other",
        "@客户/项目甲|new\n",
        "@客户/项目甲",
    ],
)
def test_unknown_or_non_exact_no_space_message_is_rejected(raw):
    with pytest.raises(ProtocolError):
        parse_session_message(raw)


@pytest.mark.parametrize(
    "raw",
    [
        "@客户/项目甲|new 附加正文",
        "@客户/项目甲|create 附加正文",
    ],
)
def test_control_with_body_never_executes(raw):
    with pytest.raises(ProtocolError, match="禁止字符"):
        parse_session_message(raw)


def test_old_slash_commands_are_plain_body():
    assert parse_session_message("@客户/项目甲 /new").body == "/new"
    assert parse_session_message("@客户/项目甲 /create").body == "/create"
