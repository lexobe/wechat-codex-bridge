import assert from "node:assert/strict";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import {
  BridgePolicy,
  PolicyStore,
  payloadHash,
} from "../openclaw/bridge-policy/policy.js";

function fixture(timeout = 60_000) {
  const dir = mkdtempSync(join(tmpdir(), "wechat-policy-"));
  const store = new PolicyStore(join(dir, "bridge.db"));
  const policy = new BridgePolicy({
    store,
    authorizedRecipients: ["wxid-owner"],
    approvalTimeoutMs: timeout,
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

test("全局分发闸门在唤醒 Codex 前拒绝未授权发送者", () => {
  const { store, policy } = fixture();
  assert.deepEqual(
    policy.beforeDispatch(
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
    policy.beforeDispatch(
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

test("微信账号标识比较不受 OpenClaw 小写规范化影响", () => {
  const dir = mkdtempSync(join(tmpdir(), "wechat-policy-case-"));
  const store = new PolicyStore(join(dir, "bridge.db"));
  const policy = new BridgePolicy({
    store,
    authorizedRecipients: ["WxId-Owner"],
  });
  assert.equal(
    policy.beforeDispatch(
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

test("腾讯微信缺少 senderId 时从 conversationId 解析私聊发送者", () => {
  const { store, policy } = fixture();
  assert.equal(
    policy.beforeDispatch(
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
