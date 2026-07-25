# 微信—Codex 最小通信闭环

这是一个已经在本机跑通的最小原型：

```text
你的微信私聊
  → 腾讯 OpenClaw 微信插件（收取、路由、投递）
  → 本仓库策略插件（授权、幂等、确认、回执）
  → OpenClaw Codex harness
  → Codex 原生 App Server 线程
  → 腾讯 OpenClaw 微信插件
  → 你的微信私聊
```

OpenClaw 只保留渠道、会话路由、批准和投递职责；Codex 是内部决策与执行单元。
中间没有另建中央业务服务。SQLite 只用于持久化、幂等和一次性确认，不承载业务
判断。

## 三步开始

克隆公开仓库并进入目录：

```bash
git clone https://github.com/lexobe/wechat-codex-bridge.git
cd wechat-codex-bridge
```

安装固定版本组件并写入安全配置：

```bash
./bin/wechat-codex-bridge install
```

完成独立 Codex OAuth 和微信二维码登录，然后启动：

```bash
./bin/wechat-codex-bridge login
./bin/wechat-codex-bridge run
```

首次扫码后仍需按“微信扫码与唯一联系人授权”一节核对并写入唯一白名单联系人。
脚本刻意不自动猜测或授权微信身份。

命令入口保持很小：

- `install`：安装精确锁定版本；已存在的正确版本会跳过。
- `configure`：写入安全配置；重复运行不会清空已有联系人白名单。
- `login`：启动 Codex OAuth 和微信扫码。
- `check`：核对四项版本、配置、策略插件和自动升级开关。
- `run`：检查通过后以前台模式启动，任何版本漂移都会拒绝运行。
- `version`：显示完整版本锁。

## 首版范围

当前只支持一个人工核准的微信私聊和纯文本：

- 同一授权私聊中的直接回复可以自动发回。
- 非白名单入站和出站立即拒绝。
- 主动发送、提醒和其他外部动作必须取得一次性明确批准。
- 批准只绑定精确目标、精确正文和有效期；拒绝、过期、正文变化或复用均失败。
- 相同入站消息编号不会重复唤醒；相同出站请求只保留一次投递结果。
- Codex 固定为 `openai/gpt-5.6-sol`，guardian 模式、只读沙箱、关闭网络代理，
  且运行时选择失败时不回退到 OpenClaw 自身模型循环。
- bundled provider 发现使用严格 allowlist，只加载配置中的四个插件。

首版不支持群聊、图片、语音、文件、定时任务、后台常驻服务和真实主动提醒。
主动发送确认链路已经通过自动化测试，并在本机完成过一次人工授权的真实投递；
首版不提供定时或无人值守主动发送。

## 仓库内容

- `openclaw/bridge-policy/`：OpenClaw 运行时策略插件。
- `openclaw/openclaw.patch.template.json5`：无账号、令牌和真实路径的配置模板。
- `src/wechat_codex_bridge/`：可独立测试的 Python 契约、模拟 Codex 和模拟网关。
- `tests-node/`：运行时策略测试。
- `tests/`：Python 契约测试。
- `var/bridge.db`：运行后生成的本地 SQLite；已被 Git 忽略。
- `bin/wechat-codex-bridge`：固定版本的安装、配置、检查和启动入口。
- `.github/workflows/ci.yml`：公开仓库的 Python 与 Node 持续集成。

## 固定版本

- Node.js `24.15.0`
- OpenClaw `2026.7.1-2`
- `@openclaw/codex` `2026.7.1-1`
- `@tencent-weixin/openclaw-weixin` `2.4.6`

Codex 插件没有 `2026.7.1-2` 发布版，因此使用与 OpenClaw
`2026.7.1-2` 兼容的 `2026.7.1-1`，其 peer dependency 接受
OpenClaw `>=2026.7.1`。

## 不自动升级

本组件把 OpenClaw 当作经过验收的内部部件，不追随最新版本：

1. 安装命令只接受上述精确版本，不使用 `latest`、`stable` 或范围版本。
2. OpenClaw 配置显式设置 `update.auto.enabled=false`。
3. `run` 额外设置 `OPENCLAW_NO_AUTO_UPDATE=1`。
4. 每次启动前检查 OpenClaw、Node、Codex 插件和微信插件的精确版本。
5. 任一版本发生漂移时拒绝启动，不自动修复、不自动升级。
6. 仓库不配置 Dependabot 或其他自动版本升级机器人。

只有在当前功能失效、存在必须修复的安全问题，或维护者主动发起兼容性验收时，
才应在独立分支更新版本锁。更新必须重新跑自动化测试和真实两轮微信验收后才能
合并。

## 从零安装

优先使用前面的组件命令。下面是等价的人工步骤，供审计和故障恢复。它使用用户
目录 `~/.openclaw`，不修改系统 Node，也不注册后台服务。
先把仓库绝对路径保存到一个任务专用变量：

```bash
export WECHAT_CODEX_PROJECT="/你的绝对路径/WeChat 通信"
```

安装固定版 OpenClaw 和受支持的 Node：

```bash
curl -fsSL --proto '=https' --tlsv1.2 https://openclaw.ai/install-cli.sh \
  | bash -s -- --prefix "$HOME/.openclaw" --version 2026.7.1-2 \
  --no-onboard
```

建立本地配置，不安装 daemon：

```bash
"$HOME/.openclaw/bin/openclaw" setup \
  --baseline --non-interactive --accept-risk --no-install-daemon \
  --skip-ui --workspace "$WECHAT_CODEX_PROJECT"
```

安装三个插件：

```bash
"$HOME/.openclaw/bin/openclaw" plugins install \
  '@openclaw/codex@2026.7.1-1'
"$HOME/.openclaw/bin/openclaw" plugins install \
  '@tencent-weixin/openclaw-weixin@2.4.6'
"$HOME/.openclaw/bin/openclaw" plugins install \
  "$WECHAT_CODEX_PROJECT/openclaw/bridge-policy"
```

将 `openclaw/openclaw.patch.template.json5` 中两个绝对路径占位符替换为仓库路径，
再把相应字段合并到 `~/.openclaw/openclaw.json`。不要把合并后的真实配置复制回
仓库。

## 独立 Codex 授权

运行：

```bash
"$HOME/.openclaw/bin/openclaw" models auth \
  --agent main login --provider openai
```

在浏览器完成 OAuth 后，认证只写入 agent 级目录，不读取现有
`~/.codex` 会话。可用下面命令确认模型、运行时认证路由和无 fallback：

```bash
"$HOME/.openclaw/bin/openclaw" models status --agent main --json
```

## 微信扫码与唯一联系人授权

显示终端二维码：

```bash
"$HOME/.openclaw/bin/openclaw" channels login \
  --channel openclaw-weixin
```

用微信扫码并在手机确认。二维码过期时重新执行一次；再次失败就停止并保留终端
日志，不绕过登录流程。

首次登录时让 `authorizedRecipients` 保持空数组。发送一条发现消息后，策略会拒绝
它并将候选发送者写入 `runtime_denied_inbound`：

```bash
sqlite3 "$WECHAT_CODEX_PROJECT/var/bridge.db" \
  'SELECT sender_id, reason, received_at FROM runtime_denied_inbound;'
```

人工核对唯一联系人后，只在 `~/.openclaw/openclaw.json` 的
`authorizedRecipients` 中填写该 ID。真实微信 ID、账号 ID、二维码和令牌都不得
写入仓库。

## 启动、检查与停止

前台启动：

```bash
"$HOME/.openclaw/bin/openclaw" gateway run --verbose
```

保持该终端打开。停止时按 `Ctrl-C`；本原型不会自动常驻。

另开终端检查：

```bash
"$HOME/.openclaw/bin/openclaw" config validate
"$HOME/.openclaw/bin/openclaw" plugins inspect \
  wechat-codex-bridge-policy --runtime --json
"$HOME/.openclaw/bin/openclaw" doctor --non-interactive
"$HOME/.openclaw/bin/openclaw" channels status --json
"$HOME/.openclaw/bin/openclaw" sessions --agent main --active 30 --json
```

日志应显示 Codex agent harness 被选中。`models status` 应显示
`openai/gpt-5.6-sol`、Codex runtime 可用、fallback 为空。网络被关闭时，Codex
对远程插件目录的探测可能出现网络警告；只要 runtime、模型调用和消息投递正常，
该警告不代表回退。

## 最小验收

从已授权微信私聊依次发送：

```text
桥接测试 1：只回复收到-1
桥接测试 2：只回复收到-2
```

应分别收到 `收到-1`、`收到-2`。随后检查：

```bash
sqlite3 "$WECHAT_CODEX_PROJECT/var/bridge.db" "
SELECT '入站', COUNT(*) FROM runtime_inbound_events
UNION ALL
SELECT '会话映射', COUNT(*) FROM runtime_conversation_mappings
UNION ALL
SELECT '出站回执', COUNT(*) FROM runtime_outbound_receipts;
"
```

干净验收应为两条入站、一个会话映射、两条成功回执。两条入站的 `session_key`
必须相同；OpenClaw 会话转录中两轮 Codex App Server 的线程前缀也必须相同。

## 自动化测试

Python 3.11 或更高版本：

```bash
PYTHONPATH=src pytest
```

Node 策略测试使用本地固定版 Node：

```bash
"$HOME/.openclaw/tools/node-v24.15.0/bin/node" \
  --test tests-node/*.test.mjs
```

测试覆盖入站幂等、白名单拒绝、同会话直接回复、主动发送确认、拒绝、超时、正文
变化、批准复用、出站请求幂等，以及成功和失败回执持久化。

公开仓库的 GitHub Actions 会在每次 push 和 pull request 上运行同样的两组测试。

## 数据与凭据位置

- OpenClaw 配置：`~/.openclaw/openclaw.json`
- agent 级 Codex 认证：`~/.openclaw/agents/main/agent/openclaw-agent.sqlite`
- OpenClaw/Codex 会话：`~/.openclaw/agents/main/sessions/`
- 微信插件认证：由 OpenClaw 微信插件保存在 `~/.openclaw` 状态目录
- 桥接幂等与回执：`var/bridge.db`

不要打印或提交这些文件的内容。SQLite 中保留了联系人标识和载荷哈希，也应按
敏感本地数据处理。

## 重新登录、恢复和卸载

微信重新登录：

```bash
"$HOME/.openclaw/bin/openclaw" channels logout \
  --channel openclaw-weixin
"$HOME/.openclaw/bin/openclaw" channels login \
  --channel openclaw-weixin
```

Codex OAuth 失效时，重新执行“独立 Codex 授权”命令。若 Gateway 启动失败，先依次
运行 `config validate`、策略插件 `inspect --runtime` 和 `doctor`。若策略数据库
目录不存在：

```bash
mkdir -p "$WECHAT_CODEX_PROJECT/var"
```

卸载前先停止前台 Gateway 并退出微信。先用 dry-run 查看影响：

```bash
"$HOME/.openclaw/bin/openclaw" plugins uninstall \
  wechat-codex-bridge-policy --dry-run
"$HOME/.openclaw/bin/openclaw" uninstall --state --dry-run
```

确认目标无误后再去掉 `--dry-run`。本项目没有安装 Gateway 系统服务；不要使用
`--all`，以免误删无关工作区或应用。仓库可单独保留，`var/bridge.db` 可在停止
Gateway 后由你自行归档或删除。

## Python 契约示例

Python 层是安全契约与模拟适配器，不参与当前真实 OpenClaw 运行时：

```python
from dataclasses import replace
from wechat_codex_bridge import (
    Bridge, ControlledSendTool, InboundEvent, MockCodexClient,
    MockGateway, OutboundRequest, Store,
)

store = Store("./var/mock.db")
bridge = Bridge(store, MockCodexClient())
bridge.receive(InboundEvent(
    channel="wechat",
    conversation_key="contact:alice",
    sender="alice",
    body="请汇总今天的笔记",
    message_id="gateway-message-001",
))

gateway = MockGateway()
send = ControlledSendTool(store, gateway, {"contact:alice"})
draft = OutboundRequest(
    request_id="outbound-001",
    channel="wechat",
    recipient_key="contact:alice",
    body="已批准的提醒内容",
    purpose="reminder",
)
pending = send.request_confirmation(draft)
send.approve(pending.confirmation_id)
receipt = send.send(replace(draft, confirmation_id=pending.confirmation_id))
```

`ControlledSendTool.name` 和 `input_schema` 定义 MCP 风格的受控发送接口；
`MockGateway` 返回可测试的投递回执。

## 许可证

项目采用 MIT 许可证，可自由使用、修改和分发。根目录 `LICENSE` 保留标准英文
法律文本；本 README 和所有操作文档使用中文。
