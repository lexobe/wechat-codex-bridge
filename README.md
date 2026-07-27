# 微信—Codex 最小通信闭环

这是一个已经在本机跑通的最小原型：

```text
你的微信私聊
  → 腾讯 OpenClaw 微信插件（收取、路由、投递）
  → 本仓库策略插件（授权、幂等、目录协议、确认、回执）
  → 普通消息：OpenClaw Codex harness
    目录消息：持久 session-chat 进程 → 已准入 provider
  → Codex 原生 App Server 线程
  → 腾讯 OpenClaw 微信插件
  → 你的微信私聊
```

OpenClaw 只保留渠道、会话路由、批准和投递职责；Codex 是内部决策与执行单元。
中间没有另建中央业务服务。SQLite 只用于持久化、幂等和一次性确认，不承载业务
判断。

冻结协议和安全边界见 [`SPEC.md`](SPEC.md)，版本变化见
[`CHANGELOG.md`](CHANGELOG.md)。

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

`configure` 会把目录会话路由接入真实微信入口，但把
`sessionChat.providerMode` 保持为 `disabled`。这是有意的安全状态：当前没有
完成一次性“无正文创建 + 跨进程恢复”外部合同探针，三种目录消息会收到明确的
`ProviderNotAdmitted`，不会落到 mock、不会创建目录，也不会误用 OpenClaw 默认
会话。普通微信消息不受影响。探针通过后，由维护者显式执行
`SESSION_CHAT_PROVIDER_MODE=codex ./bin/wechat-codex-bridge configure` 才会启用。
可用绝对路径环境变量 `SESSION_CHAT_ROOT` 把目录会话工作根目录与代码目录分离，
例如 `SESSION_CHAT_ROOT="$HOME/brain"`；配置会保存其规范化绝对路径，并把版本化
X 技能安装到该 ROOT 的 `skills/x-twitter-chrome/`。

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
- `src/wechat_codex_bridge/session_*.py`：通用目录会话路由 MVP。
- `tests-node/`：运行时策略测试。
- `tests/`：Python 契约测试。
- `var/bridge.db`：运行后生成的本地 SQLite；已被 Git 忽略。
- `bin/wechat-codex-bridge`：固定版本的安装、配置、检查和启动入口。
- `.github/workflows/ci.yml`：公开仓库的 Python 与 Node 持续集成。

## 通用目录会话路由 MVP 与真实入口

`session-chat` 不属于某个特定 provider，可通过统一 adapter 接入 Codex、Claude
Code、PI 等运行时。OpenClaw 的真实 `before_dispatch` 钩子在完成私聊白名单校验
后，把目录协议消息交给一个随 Gateway 生命周期保持的 Python JSONL 进程，因此
每个授权发送者的 active 状态能跨多条微信消息保留。普通消息继续走原有 Codex
harness，`message_received` 只做既有入站审计。

真实运行进程从不注册 `MockSessionProvider`；mock 只存在于自动化测试。仓库已经
实现 `CodexAppServerProvider`：它复用 OpenClaw 官方 Codex 插件的 OAuth 代理和
App Server 客户端，使用原生 thread ID，不读取用户的 `~/.codex`。固定版 Codex
`0.144.3` 的纯 `thread/start` 只建立内存对象，因此 runner 随后调用不含用户正文
的 `thread/name/set`，用固定名称 `session-chat` 提交空 thread；它不启动 turn、
不调用模型。真实探针已验证关闭 App Server 后，新进程能按同一 ID 恢复，且 cwd
完全一致。

工作根目录 `ROOT` 只作为安全边界，不是会话对象。ROOT 下任意深度的安全目录可
保存自己的最小绑定：

```json
{
  "schema_version": 1,
  "provider": "mock",
  "session_id": "mock-session-1"
}
```

微信文本协议只有三种形式：

```text
@客户/项目甲 继续处理这个问题
@客户/项目甲|new
@客户/新项目|create
```

规则如下：

- 原始消息的字符位置 0 必须是 ASCII `@`，不 trim，也不从中间搜索。
- 普通消息以第一个 ASCII 空格分隔路径；后续正文原样传递，正文可以包含任意
  `|`、`/new`、`/create`、代码和换行。
- `|new` 只用于已经存在的目录：无绑定时建立首个 session，有有效绑定时换成新
  session。
- `|create` 只用于不存在的安全相对路径：显式建立缺失父目录和目标目录，再建立
  首个 session。
- 普通消息指向不存在目录时严格拒绝，不创建，也不回落。
- 已存在目录没有绑定时，有旧 active 才把正文原样回落给旧 active；没有旧 active
  就拒绝。配置损坏或恢复失败时始终拒绝，不能回落。
- active 只按授权发送者保存在内存，进程重启后清空；目录绑定继续保留。
- 控制事务在调用 provider 前先持久化 `IN_PROGRESS`。provider 已返回 ID、但后续
  持久化或配置提交不能确认时，进入 `UNKNOWN_AFTER_PROVIDER_CREATE`；保留旧
  active、旧绑定以及 `|create` 已建立的目录，相同消息编号绝不自动再创建。
- 未授权发送者在调用 Python 进程前就被拒绝；缺少消息幂等编号也直接故障关闭。
- `before_dispatch` 本身不携带 OpenClaw 消息编号；策略优先关联此前
  `message_received` 持久化的网关编号，关联不到时才使用消息时间戳与正文哈希组成
  稳定的本地幂等编号。连时间戳也没有时拒绝投递。
- Python 进程异常、超时或返回未知错误时，策略插件吞掉该目录消息并返回安全错误，
  不回退到 OpenClaw 默认模型会话。
- provider 返回 `accepted_failed`、`accepted_unknown`、`not_accepted` 或
  `interrupted` 时不会包装成成功，也不会把不完整回复发回微信。

最小本地示例不会访问微信、账号或网络：

```python
from pathlib import Path
from wechat_codex_bridge import (
    MockSessionGateway,
    MockSessionProvider,
    SenderKey,
    SessionChatRouter,
    Store,
)

root = Path("./workspace")
root.mkdir(exist_ok=True)
provider = MockSessionProvider()
router = SessionChatRouter(
    root=root,
    store=Store("./var/session-chat.db"),
    providers=[provider],
    default_provider="mock",
)
owner = SenderKey("wechat", "main", "owner")
gateway = MockSessionGateway(router, frozenset({owner}))

gateway.receive(
    channel="wechat",
    account_id="main",
    sender_id="owner",
    message_id="create-1",
    body="@客户/项目甲|create",
)
result = gateway.receive(
    channel="wechat",
    account_id="main",
    sender_id="owner",
    message_id="message-1",
    body="@客户/项目甲 继续处理",
)
print(result.reply)
```

该 MVP 使用单后端进程内锁；同一个 ROOT 不允许多个写实例。SQLite
`session_control_operations` 保存控制事务与可能 orphan 的审计状态，
`session_inbound_routes` 防止普通消息重复执行。它们都不替代目录绑定，也不保存
current。

`SessionChatRouter.inspect_control_operation()` 提供本机管理员只读核对入口。
`UNKNOWN_AFTER_PROVIDER_CREATE` 或无法更新未知标记而遗留的 `IN_PROGRESS` 均不得
自动重试。公开默认配置仍保持 provider 未启用；本机验收环境的 Codex provider
已经通过无正文创建、`thread/name/set` 持久化、跨进程恢复、稳定 cwd 和单轮执行
探针。MVP 不自动采纳或删除 orphan；后续 adapter 只有在能验证精确 session、cwd
和删除回执时，才可增加权限受限的人工处置工具，且不能作为微信命令暴露。

### X/Twitter 技能与 Codex 准入

仓库的 `skills/x-twitter-chrome/SKILL.md` 位于 OpenClaw agent workspace 的标准
`skills/` 目录中。配置同时把该文件作为 `requiredCodexSkill` 传给
session-chat；任何名为 `codex` 的 provider 除通用六项合同外，还必须证明新建和
恢复的目录会话都能发现该 workspace 技能。真实 provider 在每次 `turn/start`
输入中显式附加该 skill，因此它不是只写在 README 里的约定。

X 能力与核心目录会话准入分开报告。Codex 动态工具配置仍排除宽泛的
`browser` 和 `computer`；`configure` 只把 Codex Desktop 已有的 Chrome 插件所需
浏览器控制服务投影到隔离 Codex Home，并把可用后端收窄为 `chrome`。它不会共享
用户的 Codex 线程、切换浏览器、自动登录或启用 Computer Use。前置条件是 Codex
Desktop 的 Chrome 插件已经连接到用户当前登录的 Chrome。

本地启动探针只确认隔离运行时已装载 Chrome 只读工具；真实页面读取由用户发送链接
后验收。当前探针报告：

```json
{
  "skill_discoverable": true,
  "same_chrome_read_access": true,
  "confirmed_x_writes": false,
  "provider_admitted": true,
  "ready": true,
  "x_ready": false
}
```

这表示核心 Codex 目录会话和同一 Chrome 的只读入口已装载；`x_ready` 仍为
`false`，因为点赞、回复、转发、发布、关注和私信尚未取得与精确动作绑定的一次性
批准。缺少 Chrome 连接时只读任务也必须故障关闭，不得改用其他浏览器、Jina、API
或自动登录。Claude Code 和 PI 不需要伪装支持这项能力。

### 部署当前集成

仓库代码更新后，已安装的旧策略插件不会自动升级。由维护者明确执行下面命令，才会
把本地策略插件复制更新、写入目录路由配置并重启前台 Gateway：

```bash
./bin/wechat-codex-bridge install
./bin/wechat-codex-bridge configure
./bin/wechat-codex-bridge check
./bin/wechat-codex-bridge run
```

OpenClaw、Codex 插件和微信插件严格锁定版本。不要直接编辑 JSON 把
`providerMode` 改成 `codex`。启用后，`check`（以及调用它的 `run`）会实际创建一个
位于 `ROOT/.session-chat-contract-probe` 的只读探针线程，使用新进程恢复同一
thread、核对 cwd，并执行一轮固定回执验证；任一步失败都会拒绝启动：

```bash
SESSION_CHAT_PROVIDER_MODE=codex ./bin/wechat-codex-bridge configure
./bin/wechat-codex-bridge check
./bin/wechat-codex-bridge run
```

合同探针不会写 `.session-chat.json` 或业务目录绑定，但会在 Codex 中留下一个
`session-chat` 探针线程作为可审计证据。

若代码位于 `~/Code/wechat-mcp`、工作目录使用 `~/brain`，执行：

```bash
SESSION_CHAT_PROVIDER_MODE=codex \
SESSION_CHAT_ROOT="$HOME/brain" \
./bin/wechat-codex-bridge configure
```

启用后的微信验收顺序：

```text
@验收/项目甲|create
@验收/项目甲 只回复：目录收到-1
@验收/项目甲 只回复：目录收到-2
@验收/项目甲|new
@验收/项目甲 只回复：新会话收到
```

第一条只创建目录和空 thread，不携带正文；第二、三条必须复用同一
`.session-chat.json` 中的 thread ID；第四条显式更换 thread；第五条使用新 thread。
不要在仓库根目录创建 `.session-chat.json`。

目录回复由策略插件绑定到该入站消息的一次性精确放行凭证，再交给腾讯渠道的原生
`sendText` 链路。授权、幂等和持久化键使用规范化联系人标识；实际投递必须保留
入站事件中的原始大小写，以便腾讯插件取得匹配的 `contextToken`。只有
`message_sent` 成功事件或带有效消息编号的渠道返回才能形成成功回执；同一入站
编号不会自动重复发送。

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
变化、批准复用、出站请求幂等、成功和失败回执持久化，以及目录会话 grammar、
路径安全、配置三态、回落与失败关闭、`|new`、`|create`、重启、未知事务、真实
Node→Python 入口和 Codex 的 X/Chrome 硬性准入。

公开仓库的 GitHub Actions 会在每次 push 和 pull request 上运行同样的两组测试。

## 数据与凭据位置

- OpenClaw 配置：`~/.openclaw/openclaw.json`
- agent 级 Codex 认证：`~/.openclaw/agents/main/agent/openclaw-agent.sqlite`
- OpenClaw/Codex 会话：`~/.openclaw/agents/main/sessions/`
- 微信插件认证：由 OpenClaw 微信插件保存在 `~/.openclaw` 状态目录
- 桥接幂等与回执：`var/bridge.db`
- 目录路由幂等与控制事务：`var/session-chat.db`

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

Python 层包含真实 OpenClaw 目录入口所调用的路由运行时；下面仅展示与外部服务隔离
的基础桥接模拟适配器：

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
