import { createHash, randomUUID } from "node:crypto";
import { DatabaseSync } from "node:sqlite";

const SCHEMA = `
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runtime_conversation_mappings (
  channel TEXT NOT NULL,
  account_id TEXT NOT NULL,
  sender_id TEXT NOT NULL,
  session_key TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (channel, account_id, sender_id)
);

CREATE TABLE IF NOT EXISTS runtime_inbound_events (
  channel TEXT NOT NULL,
  account_id TEXT NOT NULL,
  sender_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  session_key TEXT NOT NULL,
  body_hash TEXT NOT NULL,
  received_at TEXT NOT NULL,
  PRIMARY KEY (channel, account_id, sender_id, message_id)
);

CREATE TABLE IF NOT EXISTS runtime_denied_inbound (
  channel TEXT NOT NULL,
  account_id TEXT NOT NULL,
  sender_id TEXT NOT NULL,
  message_id TEXT NOT NULL,
  reason TEXT NOT NULL,
  received_at TEXT NOT NULL,
  PRIMARY KEY (channel, account_id, sender_id, message_id)
);

CREATE TABLE IF NOT EXISTS runtime_pending_actions (
  action_id TEXT PRIMARY KEY,
  request_id TEXT NOT NULL UNIQUE,
  session_key TEXT,
  target_key TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  state TEXT NOT NULL CHECK (
    state IN ('pending', 'approved', 'consumed', 'rejected', 'expired')
  ),
  expires_at_ms INTEGER NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS runtime_outbound_receipts (
  receipt_key TEXT PRIMARY KEY,
  session_key TEXT,
  channel TEXT NOT NULL,
  target_key TEXT NOT NULL,
  payload_hash TEXT NOT NULL,
  success INTEGER NOT NULL CHECK (success IN (0, 1)),
  gateway_message_id TEXT,
  error TEXT,
  created_at TEXT NOT NULL
);
`;

function text(value) {
  return typeof value === "string" ? value.trim() : "";
}

function identity(value) {
  return text(value).toLowerCase();
}

export function isSessionRouteCandidate(value) {
  if (typeof value !== "string" || !value.includes("@")) return false;
  if (value.startsWith("@")) return true;
  const withoutEmailAddresses = value.replace(
    /\b[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}\b/giu,
    "",
  );
  return withoutEmailAddresses.includes("@");
}

function senderIdentity(event, ctx) {
  const direct = identity(
    ctx.senderId || event.senderId || ctx.conversationId || event.conversationId
  );
  if (direct) return direct;
  const sessionKey = text(ctx.sessionKey || event.sessionKey);
  const match = sessionKey.match(/:direct:(.+)$/i);
  return match ? identity(match[1]) : "";
}

function isGroupEvent(event) {
  const metadata =
    event?.metadata && typeof event.metadata === "object" ? event.metadata : {};
  return (
    event?.isGroup === true ||
    metadata.isGroup === true ||
    ["group", "room"].includes(text(metadata.chatType).toLowerCase())
  );
}

function nowIso() {
  return new Date().toISOString();
}

export function payloadHash(target, body) {
  return createHash("sha256")
    .update(JSON.stringify({ target: identity(target), body: text(body) }))
    .digest("hex");
}

export function normalizeMessageToolCall(params = {}) {
  const action = text(params.action || "send").toLowerCase();
  if (!["send", "send_message", "reminder"].includes(action)) {
    return null;
  }
  const target = identity(
    params.target ?? params.to ?? params.recipient_key ?? params.recipient
  );
  const body = text(params.message ?? params.text ?? params.body);
  const requestId = text(
    params.request_id ?? params.requestId ?? params.idempotency_key
  );
  if (!body) {
    return null;
  }
  return {
    target,
    body,
    requestId: requestId || randomUUID(),
    purpose: action === "reminder" ? "reminder" : "message",
  };
}

export class PolicyStore {
  constructor(dbPath) {
    this.db = new DatabaseSync(dbPath);
    this.db.exec(SCHEMA);
  }

  close() {
    this.db.close();
  }

  mapConversation({ channel, accountId, senderId, sessionKey }) {
    const now = nowIso();
    this.db
      .prepare(`
        INSERT INTO runtime_conversation_mappings
          (channel, account_id, sender_id, session_key, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel, account_id, sender_id) DO UPDATE SET
          session_key = excluded.session_key,
          updated_at = excluded.updated_at
      `)
      .run(channel, accountId, senderId, sessionKey, now, now);
  }

  recordInbound({ channel, accountId, senderId, messageId, sessionKey, body }) {
    const result = this.db
      .prepare(`
        INSERT INTO runtime_inbound_events
          (channel, account_id, sender_id, message_id, session_key, body_hash, received_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel, account_id, sender_id, message_id) DO NOTHING
      `)
      .run(
        channel,
        accountId,
        senderId,
        messageId,
        sessionKey,
        payloadHash(senderId, body),
        nowIso(),
      );
    return Number(result.changes) === 1;
  }

  recordDeniedInbound({ channel, accountId, senderId, messageId, reason }) {
    if (!senderId || !messageId) return;
    this.db
      .prepare(`
        INSERT INTO runtime_denied_inbound
          (channel, account_id, sender_id, message_id, reason, received_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(channel, account_id, sender_id, message_id) DO NOTHING
      `)
      .run(channel, accountId, senderId, messageId, reason, nowIso());
  }

  findInboundMessageId({
    channel,
    accountId,
    senderId,
    sessionKey,
    body,
  }) {
    if (!senderId || !sessionKey) return null;
    const cutoff = new Date(Date.now() - 30_000).toISOString();
    const row = this.db
      .prepare(`
        SELECT message_id
        FROM runtime_inbound_events
        WHERE channel = ? AND account_id = ? AND sender_id = ?
          AND session_key = ? AND body_hash = ? AND received_at >= ?
        ORDER BY received_at DESC
        LIMIT 1
      `)
      .get(
        channel,
        accountId,
        senderId,
        sessionKey,
        payloadHash(senderId, body),
        cutoff,
      );
    return row ? String(row.message_id) : null;
  }

  isDirectReply({ channel, accountId, target, sessionKey }) {
    if (!sessionKey) return false;
    const row = this.db
      .prepare(`
        SELECT 1 FROM runtime_conversation_mappings
        WHERE channel = ? AND sender_id = ? AND session_key = ?
      `)
      .get(channel, identity(target), sessionKey);
    return Boolean(row);
  }

  recipientForSession(sessionKey) {
    if (!sessionKey) return null;
    const row = this.db
      .prepare(`
        SELECT sender_id
        FROM runtime_conversation_mappings
        WHERE session_key = ?
        ORDER BY updated_at DESC
        LIMIT 1
      `)
      .get(sessionKey);
    return row ? String(row.sender_id) : null;
  }

  createPending({ requestId, sessionKey, target, body, timeoutMs }) {
    const existing = this.db
      .prepare(`
        SELECT action_id, target_key, payload_hash, state, expires_at_ms
        FROM runtime_pending_actions WHERE request_id = ?
      `)
      .get(requestId);
    const hash = payloadHash(target, body);
    if (existing) {
      if (existing.target_key !== target || existing.payload_hash !== hash) {
        throw new Error("出站请求编号已绑定到其他目标或正文");
      }
      return {
        actionId: existing.action_id,
        state: existing.state,
        expiresAtMs: existing.expires_at_ms,
      };
    }
    const actionId = randomUUID();
    const now = nowIso();
    const expiresAtMs = Date.now() + timeoutMs;
    this.db
      .prepare(`
        INSERT INTO runtime_pending_actions
          (action_id, request_id, session_key, target_key, payload_hash, state,
           expires_at_ms, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)
      `)
      .run(actionId, requestId, sessionKey || null, target, hash, expiresAtMs, now, now);
    return { actionId, state: "pending", expiresAtMs };
  }

  resolve(actionId, decision) {
    const next = decision === "allow-once" ? "approved" : "rejected";
    const result = this.db
      .prepare(`
        UPDATE runtime_pending_actions
        SET state = ?, updated_at = ?
        WHERE action_id = ? AND state = 'pending'
      `)
      .run(next, nowIso(), actionId);
    return Number(result.changes) === 1;
  }

  consumeApproval({ target, body, sessionKey }) {
    const hash = payloadHash(target, body);
    const sql = sessionKey
      ? `
        SELECT action_id, expires_at_ms
        FROM runtime_pending_actions
        WHERE target_key = ? AND payload_hash = ? AND state = 'approved'
          AND (session_key IS NULL OR session_key = ?)
        ORDER BY created_at
        LIMIT 1
      `
      : `
        SELECT action_id, expires_at_ms
        FROM runtime_pending_actions
        WHERE target_key = ? AND payload_hash = ? AND state = 'approved'
        ORDER BY created_at
        LIMIT 1
      `;
    const row = sessionKey
      ? this.db.prepare(sql).get(target, hash, sessionKey)
      : this.db.prepare(sql).get(target, hash);
    if (!row) return false;
    if (Number(row.expires_at_ms) <= Date.now()) {
      this.db
        .prepare(`
          UPDATE runtime_pending_actions
          SET state = 'expired', updated_at = ?
          WHERE action_id = ? AND state = 'approved'
        `)
        .run(nowIso(), row.action_id);
      return false;
    }
    const result = this.db
      .prepare(`
        UPDATE runtime_pending_actions
        SET state = 'consumed', updated_at = ?
        WHERE action_id = ? AND state = 'approved'
      `)
      .run(nowIso(), row.action_id);
    return Number(result.changes) === 1;
  }

  consumeSessionReply({ target, body }) {
    const hash = payloadHash(target, body);
    const row = this.db
      .prepare(`
        SELECT action_id, request_id, expires_at_ms
        FROM runtime_pending_actions
        WHERE target_key = ? AND payload_hash = ? AND state = 'approved'
          AND request_id LIKE 'session-reply:%'
        ORDER BY created_at
        LIMIT 1
      `)
      .get(identity(target), hash);
    if (!row) return null;
    if (Number(row.expires_at_ms) <= Date.now()) {
      this.db
        .prepare(`
          UPDATE runtime_pending_actions
          SET state = 'expired', updated_at = ?
          WHERE action_id = ? AND state = 'approved'
        `)
        .run(nowIso(), row.action_id);
      return null;
    }
    const result = this.db
      .prepare(`
        UPDATE runtime_pending_actions
        SET state = 'consumed', updated_at = ?
        WHERE action_id = ? AND state = 'approved'
      `)
      .run(nowIso(), row.action_id);
    if (Number(result.changes) !== 1) return null;
    return String(row.request_id).slice("session-reply:".length);
  }

  pendingState(actionId) {
    const row = this.db
      .prepare(`
        SELECT state
        FROM runtime_pending_actions
        WHERE action_id = ?
      `)
      .get(actionId);
    return row ? String(row.state) : null;
  }

  saveReceipt({
    receiptKey,
    sessionKey,
    channel,
    target,
    body,
    success,
    gatewayMessageId,
    error,
    dedupeRecent = true,
  }) {
    const hash = payloadHash(target, body);
    if (dedupeRecent) {
      const cutoff = new Date(Date.now() - 10_000).toISOString();
      const recent = this.db
        .prepare(`
          SELECT receipt_key, gateway_message_id
          FROM runtime_outbound_receipts
          WHERE channel = ? AND target_key = ? AND payload_hash = ? AND success = ?
            AND created_at >= ?
          ORDER BY created_at DESC
          LIMIT 1
        `)
        .get(channel, identity(target), hash, success ? 1 : 0, cutoff);
      if (recent) {
        if (gatewayMessageId && !recent.gateway_message_id) {
          this.db
            .prepare(`
              UPDATE runtime_outbound_receipts
              SET receipt_key = ?, gateway_message_id = ?, error = ?
              WHERE receipt_key = ?
            `)
            .run(
              receiptKey,
              gatewayMessageId,
              error || null,
              recent.receipt_key,
            );
        }
        return false;
      }
    }
    const result = this.db
      .prepare(`
        INSERT INTO runtime_outbound_receipts
          (receipt_key, session_key, channel, target_key, payload_hash, success,
           gateway_message_id, error, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(receipt_key) DO NOTHING
      `)
      .run(
        receiptKey,
        sessionKey || null,
        channel,
        identity(target),
        hash,
        success ? 1 : 0,
        gatewayMessageId || null,
        error || null,
        nowIso(),
      );
    return Number(result.changes) === 1;
  }

  getReceipt(receiptKey) {
    return this.db
      .prepare(`
        SELECT success, gateway_message_id, error
        FROM runtime_outbound_receipts
        WHERE receipt_key = ?
      `)
      .get(receiptKey);
  }
}

export class BridgePolicy {
  constructor({
    store,
    channelId = "openclaw-weixin",
    authorizedRecipients = [],
    approvalTimeoutMs = 300_000,
    sessionClient = null,
    sendSessionReply = null,
  }) {
    this.store = store;
    this.channelId = channelId;
    this.authorizedRecipients = new Set(
      authorizedRecipients.map(identity).filter(Boolean)
    );
    this.approvalTimeoutMs = approvalTimeoutMs;
    this.sessionClient = sessionClient;
    this.sendSessionReply = sendSessionReply;
    this.sessionReplyReceipts = new Map();
  }

  isAuthorized(recipient) {
    return this.authorizedRecipients.has(identity(recipient));
  }

  async deliverSessionReply({
    accountId,
    senderId,
    recipient,
    sessionKey,
    messageId,
    body,
  }) {
    if (!this.isAuthorized(senderId)) return { handled: true };
    const receiptKey = `session-chat:${messageId}`;
    if (this.store.getReceipt(receiptKey)) {
      return { handled: true };
    }
    try {
      const grant = this.store.createPending({
        requestId: `session-reply:${receiptKey}`,
        sessionKey,
        target: identity(senderId),
        body,
        timeoutMs: 60_000,
      });
      if (grant.state !== "pending") {
        return { handled: true };
      }
      this.store.resolve(grant.actionId, "allow-once");
      if (!this.sendSessionReply) {
        return { handled: true, text: body };
      }
      const result = await this.sendSessionReply({
        channel: this.channelId,
        accountId,
        target: recipient || senderId,
        body,
      });
      if (this.store.pendingState(grant.actionId) !== "consumed") {
        throw new Error("渠道未消费一次性目录回复授权，投递已故障关闭");
      }
      if (!text(result?.messageId)) {
        throw new Error("渠道没有返回有效投递编号，投递已故障关闭");
      }
      if (!this.store.getReceipt(receiptKey)) {
        this.store.saveReceipt({
          receiptKey,
          sessionKey,
          channel: this.channelId,
          target: senderId,
          body,
          success: true,
          gatewayMessageId: result.messageId,
          dedupeRecent: false,
        });
      }
      return { handled: true };
    } catch (error) {
      if (!this.store.getReceipt(receiptKey)) {
        this.store.saveReceipt({
          receiptKey,
          sessionKey,
          channel: this.channelId,
          target: senderId,
          body,
          success: false,
          error: error instanceof Error ? error.message : String(error),
          dedupeRecent: false,
        });
      }
      return { handled: true };
    }
  }

  queueSessionReplyReceipt(target, body, receiptKey) {
    const key = `${identity(target)}\0${payloadHash(target, body)}`;
    const queue = this.sessionReplyReceipts.get(key) || [];
    queue.push(receiptKey);
    this.sessionReplyReceipts.set(key, queue);
  }

  takeSessionReplyReceipt(target, body) {
    const key = `${identity(target)}\0${payloadHash(target, body)}`;
    const queue = this.sessionReplyReceipts.get(key);
    if (!queue?.length) return null;
    const receiptKey = queue.shift();
    if (queue.length === 0) this.sessionReplyReceipts.delete(key);
    return receiptKey;
  }

  claimInbound(event, ctx) {
    if (ctx.channelId !== this.channelId) return undefined;
    const accountId = text(ctx.accountId || event.accountId || "default");
    const senderId = senderIdentity(event, ctx);
    const messageId = text(ctx.messageId || event.messageId);
    const sessionKey = text(ctx.sessionKey || event.sessionKey);
    const isGroup = isGroupEvent(event);
    if (isGroup || !senderId || !this.isAuthorized(senderId)) {
      this.store.recordDeniedInbound({
        channel: this.channelId,
        accountId,
        senderId,
        messageId,
        reason: isGroup ? "group-disabled" : "sender-not-authorized",
      });
      return { handled: true };
    }
    if (!messageId || !sessionKey) {
      return { handled: true };
    }
    const inserted = this.store.recordInbound({
      channel: this.channelId,
      accountId,
      senderId,
      messageId,
      sessionKey,
      body: event.content || event.body || "",
    });
    if (!inserted) return { handled: true };
    this.store.mapConversation({
      channel: this.channelId,
      accountId,
      senderId,
      sessionKey,
    });
    return undefined;
  }

  async beforeDispatch(event, ctx) {
    const channel = text(ctx.channelId || event.channel);
    if (channel !== this.channelId) return undefined;
    const accountId = text(ctx.accountId || "default");
    const senderId = senderIdentity(event, ctx);
    const recipient = text(
      ctx.senderId ||
      event.senderId ||
      ctx.conversationId ||
      event.conversationId ||
      senderId
    );
    const isGroup = isGroupEvent(event);
    if (!senderId || isGroup || !this.isAuthorized(senderId)) {
      this.store.recordDeniedInbound({
        channel: this.channelId,
        accountId,
        senderId,
        messageId: `${event.timestamp || Date.now()}:${payloadHash(senderId, event.content)}`,
        reason: isGroup ? "group-disabled" : "sender-not-authorized",
      });
      return { handled: true };
    }
    const body =
      typeof event.content === "string"
        ? event.content
        : typeof event.body === "string"
          ? event.body
          : "";
    if (!isSessionRouteCandidate(body)) return undefined;
    if (!this.sessionClient) {
      return {
        handled: true,
        text: "目录会话路由未启用，消息未投递。",
      };
    }
    const sessionKey = text(ctx.sessionKey || event.sessionKey);
    const correlatedMessageId = this.store.findInboundMessageId({
      channel: this.channelId,
      accountId,
      senderId,
      sessionKey,
      body,
    });
    const timestamp = Number(event.timestamp);
    const syntheticMessageId = Number.isFinite(timestamp)
      ? `dispatch:${timestamp}:${payloadHash(senderId, body)}`
      : "";
    const messageId = text(
      ctx.messageId ||
      event.messageId ||
      correlatedMessageId ||
      syntheticMessageId,
    );
    if (!messageId) {
      return {
        handled: true,
        text: "目录会话消息缺少幂等编号，已故障关闭且未投递。",
      };
    }
    if (sessionKey) {
      this.store.mapConversation({
        channel: this.channelId,
        accountId,
        senderId,
        sessionKey,
      });
    }
    try {
      const result = await this.sessionClient.route({
        channel: this.channelId,
        account_id: accountId,
        sender_id: senderId,
        message_id: messageId,
        body,
      });
      let reply;
      if (
        result.turn_status === "accepted_failed" ||
        result.turn_status === "accepted_unknown" ||
        result.turn_status === "not_accepted"
      ) {
        const statusText =
          result.turn_status === "accepted_failed"
            ? "执行失败"
            : result.turn_status === "accepted_unknown"
              ? "执行结果未知"
              : "未被接受";
        reply = `目录会话${statusText}，未把该轮标记为成功。`;
      } else if (typeof result.reply === "string" && result.reply) {
        reply = result.reply;
      } else if (
        result.outcome === "delivered" ||
        result.outcome === "fallback_to_active"
      ) {
        reply = "目录会话已接受该消息，但没有返回文本。";
      } else {
        const action =
          result.outcome === "directory_created"
            ? "目录和会话已创建并切换"
            : "新会话已创建并切换";
        reply = result.duplicate
          ? `${action}（重复请求，未再次创建）。`
          : `${action}。`;
      }
      return await this.deliverSessionReply({
        accountId,
        senderId,
        recipient,
        sessionKey,
        messageId,
        body: reply,
      });
    } catch (error) {
      const safeCodes = new Set([
        "ProtocolError",
        "PathSecurityError",
        "BindingInvalid",
        "ProviderNotAdmitted",
        "ResumeFailed",
        "NoActiveSession",
        "ControlOperationBlocked",
        "UnknownAfterProviderCreate",
        "DuplicateMessageBlocked",
        "InvalidRuntimeRequest",
      ]);
      const code = safeCodes.has(error.code) ? error.code : "RuntimeUnavailable";
      const detail = safeCodes.has(error.code)
        ? String(error.message)
        : "目录会话运行时不可用";
      return await this.deliverSessionReply({
        accountId,
        senderId,
        recipient,
        sessionKey,
        messageId,
        body: `目录会话未执行（${code}）：${detail}`,
      });
    }
  }

  observeInbound(event, ctx) {
    if (ctx.channelId !== this.channelId) return;
    const accountId = text(ctx.accountId || "default");
    const senderId = senderIdentity(event, ctx);
    const messageId = text(ctx.messageId || event.messageId);
    const sessionKey = text(ctx.sessionKey || event.sessionKey);
    if (!senderId || !messageId || !sessionKey || !this.isAuthorized(senderId)) return;
    const inserted = this.store.recordInbound({
      channel: this.channelId,
      accountId,
      senderId,
      messageId,
      sessionKey,
      body: event.content || "",
    });
    if (inserted) {
      this.store.mapConversation({
        channel: this.channelId,
        accountId,
        senderId,
        sessionKey,
      });
    }
  }

  beforeToolCall(event, ctx) {
    if (!["message", "messages_send", "wechat.send_message"].includes(event.toolName)) {
      return undefined;
    }
    const call = normalizeMessageToolCall(event.params);
    if (!call) {
      return { block: true, blockReason: "出站消息参数不完整或动作不受支持" };
    }
    const requestedSession = text(
      event.params.session_key ?? event.params.sessionKey
    );
    const target = identity(
      call.target ||
      this.store.recipientForSession(requestedSession || ctx.sessionKey)
    );
    if (!target || !this.isAuthorized(target)) {
      return { block: true, blockReason: `收件人未获授权：${target || "未知目标"}` };
    }
    const directSession =
      !requestedSession || requestedSession === text(ctx.sessionKey);
    if (
      call.purpose === "message" &&
      directSession &&
      this.store.recipientForSession(ctx.sessionKey) === target
    ) {
      const directGrant = this.store.createPending({
        requestId: `direct:${event.toolCallId || call.requestId}`,
        sessionKey: ctx.sessionKey,
        target,
        body: call.body,
        timeoutMs: 60_000,
      });
      if (directGrant.state === "pending") {
        this.store.resolve(directGrant.actionId, "allow-once");
      }
      return undefined;
    }
    let pending;
    try {
      pending = this.store.createPending({
        requestId: call.requestId || event.toolCallId || randomUUID(),
        sessionKey: ctx.sessionKey,
        target,
        body: call.body,
        timeoutMs: this.approvalTimeoutMs,
      });
    } catch (error) {
      return { block: true, blockReason: String(error.message || error) };
    }
    if (pending.state !== "pending") {
      return { block: true, blockReason: "该出站请求编号已经处理，禁止复用" };
    }
    return {
      requireApproval: {
        title: "批准微信外发消息",
        description: `目标：${target}\n正文：${call.body}`,
        severity: "high",
        timeoutMs: this.approvalTimeoutMs,
        timeoutBehavior: "deny",
        timeoutReason: "微信外发批准已超时",
        allowedDecisions: ["allow-once", "deny"],
        pluginId: "wechat-codex-bridge-policy",
        onResolution: async (decision) => {
          this.store.resolve(pending.actionId, decision);
        },
      },
    };
  }

  messageSending(event, ctx) {
    if (ctx.channelId !== this.channelId) return undefined;
    const target = identity(event.to);
    const body = text(event.content);
    const accountId = text(ctx.accountId || "default");
    if (!target || !body || !this.isAuthorized(target)) {
      return { cancel: true, cancelReason: "目标未获授权或正文为空" };
    }
    const sessionReceiptKey = this.store.consumeSessionReply({ target, body });
    if (sessionReceiptKey) {
      this.queueSessionReplyReceipt(target, body, sessionReceiptKey);
      return undefined;
    }
    if (
      this.store.isDirectReply({
        channel: this.channelId,
        accountId,
        target,
        sessionKey: ctx.sessionKey,
      })
    ) {
      return undefined;
    }
    if (this.store.consumeApproval({ target, body, sessionKey: ctx.sessionKey })) {
      return undefined;
    }
    return {
      cancel: true,
      cancelReason: "主动发送必须具有与目标和正文精确绑定的一次性批准",
    };
  }

  messageSent(event, ctx) {
    if (ctx.channelId !== this.channelId) return;
    const sessionReceiptKey = this.takeSessionReplyReceipt(
      event.to,
      event.content,
    );
    const receiptKey =
      sessionReceiptKey ||
      text(event.messageId) ||
      `local:${randomUUID()}`;
    this.store.saveReceipt({
      receiptKey,
      sessionKey: ctx.sessionKey,
      channel: this.channelId,
      target: event.to,
      body: event.content,
      success: event.success === true,
      gatewayMessageId: event.messageId,
      error: event.error,
      dedupeRecent: !sessionReceiptKey,
    });
  }
}
