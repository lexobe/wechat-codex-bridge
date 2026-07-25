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

function senderIdentity(event, ctx) {
  const direct = identity(
    ctx.senderId || event.senderId || ctx.conversationId || event.conversationId
  );
  if (direct) return direct;
  const sessionKey = text(ctx.sessionKey || event.sessionKey);
  const match = sessionKey.match(/:direct:(.+)$/i);
  return match ? identity(match[1]) : "";
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

  saveReceipt({
    receiptKey,
    sessionKey,
    channel,
    target,
    body,
    success,
    gatewayMessageId,
    error,
  }) {
    const hash = payloadHash(target, body);
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
}

export class BridgePolicy {
  constructor({
    store,
    channelId = "openclaw-weixin",
    authorizedRecipients = [],
    approvalTimeoutMs = 300_000,
  }) {
    this.store = store;
    this.channelId = channelId;
    this.authorizedRecipients = new Set(
      authorizedRecipients.map(identity).filter(Boolean)
    );
    this.approvalTimeoutMs = approvalTimeoutMs;
  }

  isAuthorized(recipient) {
    return this.authorizedRecipients.has(identity(recipient));
  }

  claimInbound(event, ctx) {
    if (ctx.channelId !== this.channelId) return undefined;
    const accountId = text(ctx.accountId || event.accountId || "default");
    const senderId = senderIdentity(event, ctx);
    const messageId = text(ctx.messageId || event.messageId);
    const sessionKey = text(ctx.sessionKey || event.sessionKey);
    const metadata = event.metadata && typeof event.metadata === "object"
      ? event.metadata
      : {};
    const isGroup =
      metadata.isGroup === true ||
      ["group", "room"].includes(text(metadata.chatType).toLowerCase());
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

  beforeDispatch(event, ctx) {
    const channel = text(ctx.channelId || event.channel);
    if (channel !== this.channelId) return undefined;
    const accountId = text(ctx.accountId || "default");
    const senderId = senderIdentity(event, ctx);
    const isGroup = event.isGroup === true;
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
    return undefined;
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
    const receiptKey =
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
    });
  }
}
