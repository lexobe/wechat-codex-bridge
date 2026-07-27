import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";

import {
  BridgePolicy,
  PolicyStore,
  isSessionRouteCandidate,
  payloadHash,
} from "../openclaw/bridge-policy/policy.js";
import { SessionChatClient } from "../openclaw/bridge-policy/session-chat-client.js";
import { createTurnCollector } from "../openclaw/codex-provider-runner.mjs";

function fixture(
  timeout = 60_000,
  sessionClient = null,
  sendSessionReply = null,
) {
  const dir = mkdtempSync(join(tmpdir(), "wechat-policy-"));
  const store = new PolicyStore(join(dir, "bridge.db"));
  const policy = new BridgePolicy({
    store,
    authorizedRecipients: ["wxid-owner"],
    approvalTimeoutMs: timeout,
    sessionClient,
    sendSessionReply,
  });
  return { store, policy };
}

const inboundContext = {
  channelId: "openclaw-weixin",
  accountId: "main",
  senderId: "wxid-owner",
  messageId: "in-1",
  sessionKey: "agent:main:wechat:owner",
};

test("重复入站消息只放行一次并保持一个会话映射", () => {
  const { store, policy } = fixture();
  const event = { content: "你好" };
  assert.equal(policy.claimInbound(event, inboundContext), undefined);
  assert.deepEqual(policy.claimInbound(event, inboundContext), { handled: true });

  const count = store.db
    .prepare("SELECT count(*) AS count FROM runtime_inbound_events")
    .get().count;
  const mappings = store.db
    .prepare("SELECT count(*) AS count FROM runtime_conversation_mappings")
    .get().count;
  assert.equal(count, 1);
  assert.equal(mappings, 1);
  store.close();
});

test("未授权发送者和群聊在进入 Codex 前被吞掉", () => {
  const { store, policy } = fixture();
  assert.deepEqual(
    policy.claimInbound(
      { content: "攻击输入" },
      { ...inboundContext, senderId: "wxid-other", messageId: "in-2" },
    ),
    { handled: true },
  );
  assert.deepEqual(
    policy.claimInbound(
      { content: "群消息", metadata: { isGroup: true } },
      { ...inboundContext, messageId: "in-3" },
    ),
    { handled: true },
  );
  const denied = store.db
    .prepare("SELECT count(*) AS count FROM runtime_denied_inbound")
    .get().count;
  assert.equal(denied, 2);
  store.close();
});

test("全局分发闸门在唤醒 Codex 前拒绝未授权发送者", async () => {
  const { store, policy } = fixture();
  assert.deepEqual(
    await policy.beforeDispatch(
      { content: "发现消息", timestamp: 123 },
      {
        channelId: "openclaw-weixin",
        accountId: "main",
        senderId: "wxid-other",
      },
    ),
    { handled: true },
  );
  assert.equal(
    await policy.beforeDispatch(
      { content: "已授权消息", timestamp: 124 },
      {
        channelId: "openclaw-weixin",
        accountId: "main",
        senderId: "wxid-owner",
      },
    ),
    undefined,
  );
  store.close();
});

test("全局分发闸门识别 metadata 中的群聊标记", async () => {
  let routeCalls = 0;
  const sessionClient = {
    async route() {
      routeCalls += 1;
      return { outcome: "delivered", reply: "不应执行" };
    },
  };
  const { store, policy } = fixture(60_000, sessionClient);

  assert.deepEqual(
    await policy.beforeDispatch(
      {
        content: "@项目 群消息",
        metadata: { chatType: "room" },
      },
      inboundContext,
    ),
    { handled: true },
  );
  assert.equal(routeCalls, 0);
  store.close();
});

test("微信账号标识比较不受 OpenClaw 小写规范化影响", async () => {
  const dir = mkdtempSync(join(tmpdir(), "wechat-policy-case-"));
  const store = new PolicyStore(join(dir, "bridge.db"));
  const policy = new BridgePolicy({
    store,
    authorizedRecipients: ["WxId-Owner"],
  });
  assert.equal(
    await policy.beforeDispatch(
      { content: "大小写规范化" },
      {
        channelId: "openclaw-weixin",
        accountId: "main",
        senderId: "wxid-owner",
      },
    ),
    undefined,
  );
  store.close();
});

test("腾讯微信缺少 senderId 时从 conversationId 解析私聊发送者", async () => {
  const { store, policy } = fixture();
  assert.equal(
    await policy.beforeDispatch(
      { content: "conversationId 回退" },
      {
        channelId: "openclaw-weixin",
        accountId: "main",
        conversationId: "WxId-Owner",
        sessionKey: "agent:main:openclaw-weixin:main:direct:wxid-owner",
      },
    ),
    undefined,
  );
  policy.observeInbound(
    { content: "conversationId 回退", messageId: "case-1" },
    {
      channelId: "openclaw-weixin",
      accountId: "main",
      conversationId: "WxId-Owner",
      sessionKey: "agent:main:openclaw-weixin:main:direct:wxid-owner",
    },
  );
  assert.equal(
    store.recipientForSession(
      "agent:main:openclaw-weixin:main:direct:wxid-owner"
    ),
    "wxid-owner",
  );
  store.close();
});

test("真实 before_dispatch 把三种目录协议交给持久路由客户端", async () => {
  const calls = [];
  const sessionClient = {
    async route(request) {
      calls.push(request);
      if (request.body.endsWith("|create")) {
        return { outcome: "directory_created", duplicate: false, reply: null };
      }
      if (request.body.endsWith("|new")) {
        return { outcome: "session_created", duplicate: false, reply: null };
      }
      return { outcome: "delivered", duplicate: false, reply: "目录回复" };
    },
  };
  const { store, policy } = fixture(60_000, sessionClient);
  const ctx = {
    channelId: inboundContext.channelId,
    accountId: inboundContext.accountId,
    senderId: inboundContext.senderId,
    sessionKey: inboundContext.sessionKey,
  };

  assert.deepEqual(
    await policy.beforeDispatch(
      { content: "@客户/项目 正文", timestamp: 1001 },
      ctx,
    ),
    { handled: true, text: "目录回复" },
  );
  assert.deepEqual(
    await policy.beforeDispatch(
      { content: "@客户/项目|new", timestamp: 1002 },
      ctx,
    ),
    { handled: true, text: "新会话已创建并切换。" },
  );
  assert.deepEqual(
    await policy.beforeDispatch(
      { content: "@客户/新项目|create", timestamp: 1003 },
      ctx,
    ),
    { handled: true, text: "目录和会话已创建并切换。" },
  );

  assert.deepEqual(
    calls.map((call) => call.body),
    ["@客户/项目 正文", "@客户/项目|new", "@客户/新项目|create"],
  );
  assert.equal(calls[0].sender_id, "wxid-owner");
  assert.match(calls[0].message_id, /^dispatch:1001:/);
  store.close();
});

test("before_dispatch 优先复用 message_received 的真实消息编号", async () => {
  const calls = [];
  const sessionClient = {
    async route(request) {
      calls.push(request);
      return { outcome: "delivered", reply: "收到" };
    },
  };
  const { store, policy } = fixture(60_000, sessionClient);
  policy.observeInbound(
    { content: "@项目 正文", messageId: "gateway-real-1" },
    {
      channelId: inboundContext.channelId,
      accountId: inboundContext.accountId,
      senderId: inboundContext.senderId,
      sessionKey: inboundContext.sessionKey,
    },
  );

  const result = await policy.beforeDispatch(
    { content: "@项目 正文", timestamp: 1001 },
    {
      channelId: inboundContext.channelId,
      accountId: inboundContext.accountId,
      senderId: inboundContext.senderId,
      sessionKey: inboundContext.sessionKey,
    },
  );

  assert.deepEqual(result, { handled: true, text: "收到" });
  assert.equal(calls[0].message_id, "gateway-real-1");
  store.close();
});

test("目录回复经受控渠道发送并按入站编号持久化一次回执", async () => {
  const sends = [];
  const sessionClient = {
    async route() {
      return { outcome: "delivered", duplicate: false, reply: "目录回复" };
    },
  };
  let policyUnderTest;
  const { store, policy } = fixture(
    60_000,
    sessionClient,
    async (request) => {
      sends.push(request);
      assert.equal(
        policyUnderTest.messageSending(
          { to: request.target, content: request.body },
          { channelId: request.channel, accountId: request.accountId },
        ),
        undefined,
      );
      policyUnderTest.messageSent(
        {
          to: request.target,
          content: request.body,
          success: true,
          messageId: "gateway-session-1",
        },
        { channelId: request.channel, accountId: request.accountId },
      );
      return { messageId: "gateway-session-1" };
    },
  );
  policyUnderTest = policy;
  const exactCaseContext = {
    ...inboundContext,
    senderId: "WxId-Owner",
    messageId: undefined,
  };
  policy.observeInbound(
    { content: "@项目 正文", messageId: "session-route-1" },
    exactCaseContext,
  );

  assert.deepEqual(
    await policy.beforeDispatch(
      { content: "@项目 正文", timestamp: 1001 },
      exactCaseContext,
    ),
    { handled: true },
  );
  assert.deepEqual(
    await policy.beforeDispatch(
      { content: "@项目 正文", timestamp: 1001 },
      exactCaseContext,
    ),
    { handled: true },
  );

  assert.equal(sends.length, 1);
  assert.equal(sends[0].target, "WxId-Owner");
  const receipt = store.db
    .prepare(`
      SELECT success, gateway_message_id
      FROM runtime_outbound_receipts
      WHERE receipt_key = 'session-chat:session-route-1'
    `)
    .get();
  assert.equal(receipt.success, 1);
  assert.equal(receipt.gateway_message_id, "gateway-session-1");
  store.close();
});

test("目录回复未经过一次性出站授权消费时故障关闭", async () => {
  const sessionClient = {
    async route() {
      return { outcome: "delivered", duplicate: false, reply: "不会误报成功" };
    },
  };
  const { store, policy } = fixture(
    60_000,
    sessionClient,
    async () => ({ messageId: "adapter-only" }),
  );
  policy.observeInbound(
    { content: "@项目 正文", messageId: "session-route-unconsumed" },
    { ...inboundContext, messageId: undefined },
  );

  assert.deepEqual(
    await policy.beforeDispatch(
      { content: "@项目 正文" },
      { ...inboundContext, messageId: undefined },
    ),
    { handled: true },
  );
  assert.deepEqual(
    await policy.beforeDispatch(
      { content: "@项目 正文" },
      { ...inboundContext, messageId: undefined },
    ),
    { handled: true },
  );
  const receipt = store.db
    .prepare(`
      SELECT success, error
      FROM runtime_outbound_receipts
      WHERE receipt_key = 'session-chat:session-route-unconsumed'
    `)
    .get();
  assert.equal(receipt.success, 0);
  assert.match(receipt.error, /未消费一次性目录回复授权/);
  store.close();
});

test("目录回复投递失败写失败回执且相同编号不自动重试", async () => {
  const sessionClient = {
    async route() {
      return { outcome: "delivered", duplicate: false, reply: "不会收到" };
    },
  };
  let policyUnderTest;
  const { store, policy } = fixture(
    60_000,
    sessionClient,
    async (request) => {
      assert.equal(
        policyUnderTest.messageSending(
          { to: request.target, content: request.body },
          { channelId: request.channel, accountId: request.accountId },
        ),
        undefined,
      );
      policyUnderTest.messageSent(
        {
          to: request.target,
          content: request.body,
          success: false,
          error: "模拟渠道失败",
        },
        { channelId: request.channel, accountId: request.accountId },
      );
      throw new Error("模拟渠道失败");
    },
  );
  policyUnderTest = policy;
  policy.observeInbound(
    { content: "@项目 正文", messageId: "session-route-failed" },
    { ...inboundContext, messageId: undefined },
  );

  assert.deepEqual(
    await policy.beforeDispatch(
      { content: "@项目 正文" },
      { ...inboundContext, messageId: undefined },
    ),
    { handled: true },
  );
  assert.deepEqual(
    await policy.beforeDispatch(
      { content: "@项目 正文" },
      { ...inboundContext, messageId: undefined },
    ),
    { handled: true },
  );

  const receipt = store.db
    .prepare(`
      SELECT success, error
      FROM runtime_outbound_receipts
      WHERE receipt_key = 'session-chat:session-route-failed'
    `)
    .get();
  assert.equal(receipt.success, 0);
  assert.match(receipt.error, /模拟渠道失败/);
  store.close();
});

test("路由候选不 trim，前导空白或说明前缀交给协议层拒绝", async () => {
  const calls = [];
  const sessionClient = {
    async route(request) {
      calls.push(request);
      const error = new Error("路由前缀 @ 必须位于字符位置 0");
      error.code = "ProtocolError";
      throw error;
    },
  };
  const { store, policy } = fixture(60_000, sessionClient);
  for (const body of [
    " @项目 正文",
    "\n@项目 正文",
    "说明：@项目 正文",
    "普通文字@项目 正文",
  ]) {
    const result = await policy.beforeDispatch(
      { content: body },
      { ...inboundContext, messageId: `bad-${calls.length}` },
    );
    assert.equal(result.handled, true);
    assert.match(result.text, /ProtocolError/);
  }
  assert.deepEqual(calls.map((call) => call.body), [
    " @项目 正文",
    "\n@项目 正文",
    "说明：@项目 正文",
    "普通文字@项目 正文",
  ]);
  store.close();
});

test("普通正文不进入目录路由，未授权路由消息也不会调用客户端", async () => {
  let routeCalls = 0;
  const sessionClient = {
    async route() {
      routeCalls += 1;
      return {};
    },
  };
  const { store, policy } = fixture(60_000, sessionClient);
  assert.equal(
    await policy.beforeDispatch({ content: "普通微信消息" }, inboundContext),
    undefined,
  );
  assert.deepEqual(
    await policy.beforeDispatch(
      { content: "@项目 正文" },
      { ...inboundContext, senderId: "wxid-other" },
    ),
    { handled: true },
  );
  assert.equal(routeCalls, 0);
  store.close();
});

test("目录运行时错误在真实入口故障关闭且不泄露内部错误", async () => {
  const sessionClient = {
    async route() {
      throw new Error("/private/path secret");
    },
  };
  const { store, policy } = fixture(60_000, sessionClient);
  const result = await policy.beforeDispatch(
    { content: "@项目 正文" },
    inboundContext,
  );
  assert.equal(result.handled, true);
  assert.match(result.text, /RuntimeUnavailable/);
  assert.doesNotMatch(result.text, /private|secret/);
  store.close();
});

test("provider 失败和未知状态不会被包装成成功", async () => {
  for (const turnStatus of ["accepted_failed", "accepted_unknown", "not_accepted"]) {
    const sessionClient = {
      async route() {
        return {
          outcome: "delivered",
          turn_status: turnStatus,
          reply: "不得作为成功回复发送",
        };
      },
    };
    const { store, policy } = fixture(60_000, sessionClient);
    const result = await policy.beforeDispatch(
      { content: "@项目 正文" },
      { ...inboundContext, messageId: `status-${turnStatus}` },
    );
    assert.equal(result.handled, true);
    assert.match(result.text, /未把该轮标记为成功/);
    assert.doesNotMatch(result.text, /不得作为成功回复发送/);
    store.close();
  }
});

test("Codex interrupted turn 被标记为结果未知", async () => {
  const collector = createTurnCollector("thread-1", 1_000);
  collector.setTurnId("turn-1");
  collector.handle({
    method: "turn/completed",
    params: {
      threadId: "thread-1",
      turnId: "turn-1",
      turn: {
        id: "turn-1",
        status: "interrupted",
        items: [{ id: "partial", type: "agentMessage", text: "不完整回复" }],
      },
    },
  });

  assert.deepEqual(await collector.completion, {
    status: "accepted_unknown",
    reply: null,
    error: "Codex turn 被中断，结果未知",
  });
});

test("目录路由候选只识别精确前缀意图，不误伤邮箱正文", () => {
  assert.equal(isSessionRouteCandidate("@项目 正文"), true);
  assert.equal(isSessionRouteCandidate(" @项目 正文"), true);
  assert.equal(isSessionRouteCandidate("说明：@项目 正文"), true);
  assert.equal(isSessionRouteCandidate("普通文字@项目 正文"), true);
  assert.equal(isSessionRouteCandidate("联系 a@example.com"), false);
  assert.equal(isSessionRouteCandidate("联系 a@example.com，再看 @项目"), true);
  assert.equal(isSessionRouteCandidate("普通正文"), false);
});

test("真实 Node 到 Python 链路在 provider 未准入时故障关闭且不建目录", async () => {
  const dir = mkdtempSync(join(tmpdir(), "wechat-session-runtime-"));
  const root = join(dir, "root");
  const { mkdirSync, existsSync } = await import("node:fs");
  mkdirSync(root);
  const client = new SessionChatClient({
    projectRoot: process.cwd(),
    root,
    dbPath: join(dir, "session-chat.db"),
    providerMode: "disabled",
    requiredCodexSkill: resolve(
      "skills/x-twitter-chrome/SKILL.md",
    ),
    requestTimeoutMs: 5_000,
  });

  await assert.rejects(
    client.route({
      channel: "openclaw-weixin",
      account_id: "main",
      sender_id: "owner",
      message_id: "create-disabled-1",
      body: "@不得创建|create",
    }),
    (error) => error.code === "ProviderNotAdmitted",
  );
  assert.equal(existsSync(join(root, "不得创建")), false);
  client.close();
});

test("同一授权会话的直接回复自动放行", () => {
  const { store, policy } = fixture();
  policy.claimInbound({ content: "你好" }, inboundContext);
  const result = policy.messageSending(
    { to: "wxid-owner", content: "收到" },
    {
      channelId: "openclaw-weixin",
      accountId: "main",
      sessionKey: inboundContext.sessionKey,
    },
  );
  assert.equal(result, undefined);
  store.close();
});

test("出站钩子缺少 accountId 时仍以精确 session 和目标识别直接回复", () => {
  const { store, policy } = fixture();
  policy.claimInbound({ content: "你好" }, inboundContext);
  assert.equal(
    policy.messageSending(
      { to: "WxId-Owner", content: "收到" },
      {
        channelId: "openclaw-weixin",
        sessionKey: inboundContext.sessionKey,
      },
    ),
    undefined,
  );
  store.close();
});

test("同一会话的隐式目标 message 工具直接回复自动放行", () => {
  const { store, policy } = fixture();
  policy.claimInbound({ content: "你好" }, inboundContext);
  const result = policy.beforeToolCall(
    {
      toolName: "message",
      params: { action: "send", message: "收到" },
      toolCallId: "tool-direct-1",
    },
    {
      channelId: "openclaw-weixin",
      sessionKey: inboundContext.sessionKey,
    },
  );
  assert.equal(result, undefined);
  assert.equal(
    policy.messageSending(
      { to: "WxId-Owner", content: "收到" },
      { channelId: "openclaw-weixin" },
    ),
    undefined,
  );
  assert.equal(
    policy.messageSending(
      { to: "WxId-Owner", content: "收到" },
      { channelId: "openclaw-weixin" },
    ).cancel,
    true,
  );
  store.close();
});

test("非白名单出站立即拒绝", () => {
  const { store, policy } = fixture();
  const result = policy.beforeToolCall(
    {
      toolName: "message",
      params: { action: "send", target: "wxid-other", message: "你好" },
    },
    { sessionKey: inboundContext.sessionKey },
  );
  assert.equal(result.block, true);
  store.close();
});

test("主动发送需要一次性精确批准，正文变化和批准复用均失败", async () => {
  const { store, policy } = fixture();
  const toolResult = policy.beforeToolCall(
    {
      toolName: "message",
      params: {
        action: "send",
        target: "wxid-owner",
        message: "批准正文",
        request_id: "out-1",
      },
    },
    { sessionKey: "proactive-session" },
  );
  assert.ok(toolResult.requireApproval);

  assert.equal(
    policy.messageSending(
      { to: "wxid-owner", content: "批准正文" },
      { channelId: "openclaw-weixin", sessionKey: "proactive-session" },
    ).cancel,
    true,
  );

  await toolResult.requireApproval.onResolution("allow-once");
  assert.equal(
    policy.messageSending(
      { to: "wxid-owner", content: "被修改的正文" },
      { channelId: "openclaw-weixin", sessionKey: "proactive-session" },
    ).cancel,
    true,
  );
  assert.equal(
    policy.messageSending(
      { to: "wxid-owner", content: "批准正文" },
      { channelId: "openclaw-weixin", sessionKey: "proactive-session" },
    ),
    undefined,
  );
  assert.equal(
    policy.messageSending(
      { to: "wxid-owner", content: "批准正文" },
      { channelId: "openclaw-weixin", sessionKey: "proactive-session" },
    ).cancel,
    true,
  );
  store.close();
});

test("拒绝和超时均不能放行", async () => {
  const { store, policy } = fixture(1);
  const denied = policy.beforeToolCall(
    {
      toolName: "messages_send",
      params: {
        session_key: "proactive-session",
        target: "wxid-owner",
        text: "拒绝正文",
        request_id: "out-deny",
      },
    },
    { sessionKey: "proactive-session" },
  );
  await denied.requireApproval.onResolution("deny");
  assert.equal(
    policy.messageSending(
      { to: "wxid-owner", content: "拒绝正文" },
      { channelId: "openclaw-weixin", sessionKey: "proactive-session" },
    ).cancel,
    true,
  );

  const expired = policy.beforeToolCall(
    {
      toolName: "message",
      params: {
        target: "wxid-owner",
        message: "超时正文",
        request_id: "out-expired",
      },
    },
    { sessionKey: "proactive-session" },
  );
  await expired.requireApproval.onResolution("allow-once");
  await new Promise((resolve) => setTimeout(resolve, 5));
  assert.equal(
    policy.messageSending(
      { to: "wxid-owner", content: "超时正文" },
      { channelId: "openclaw-weixin", sessionKey: "proactive-session" },
    ).cancel,
    true,
  );
  store.close();
});

test("成功和失败回执均持久化且重复回执不重复插入", () => {
  const { store, policy } = fixture();
  const ctx = {
    channelId: "openclaw-weixin",
    sessionKey: inboundContext.sessionKey,
  };
  policy.messageSent(
    {
      to: "wxid-owner",
      content: "成功",
      success: true,
      messageId: "gateway-1",
    },
    ctx,
  );
  policy.messageSent(
    {
      to: "wxid-owner",
      content: "失败",
      success: false,
      messageId: "gateway-2",
      error: "模拟失败",
    },
    ctx,
  );
  policy.messageSent(
    {
      to: "wxid-owner",
      content: "成功",
      success: true,
      messageId: "gateway-1",
    },
    ctx,
  );
  const rows = store.db
    .prepare(`
      SELECT receipt_key, success, error
      FROM runtime_outbound_receipts ORDER BY receipt_key
    `)
    .all()
    .map((row) => ({ ...row }));
  assert.deepEqual(rows, [
    { receipt_key: "gateway-1", success: 1, error: null },
    { receipt_key: "gateway-2", success: 0, error: "模拟失败" },
  ]);
  store.close();
});

test("同一次投递的无编号和带编号事件合并为最终回执", () => {
  const { store, policy } = fixture();
  const ctx = { channelId: "openclaw-weixin" };
  policy.messageSent(
    { to: "wxid-owner", content: "合并", success: true },
    ctx,
  );
  policy.messageSent(
    {
      to: "WxId-Owner",
      content: "合并",
      success: true,
      messageId: "gateway-final",
    },
    ctx,
  );
  const rows = store.db
    .prepare(`
      SELECT receipt_key, gateway_message_id
      FROM runtime_outbound_receipts
      WHERE payload_hash = ?
    `)
    .all(payloadHash("wxid-owner", "合并"));
  assert.deepEqual(rows.map((row) => ({ ...row })), [
    {
      receipt_key: "gateway-final",
      gateway_message_id: "gateway-final",
    },
  ]);
  store.close();
});
