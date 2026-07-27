#!/usr/bin/env node

import { readFile, readdir } from "node:fs/promises";
import { pathToFileURL } from "node:url";
import { join, resolve } from "node:path";

function requiredString(value, name) {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`${name} 必须是非空字符串`);
  }
  return value;
}

async function readRequest() {
  const chunks = [];
  for await (const chunk of process.stdin) chunks.push(chunk);
  const raw = Buffer.concat(chunks).toString("utf8");
  const value = JSON.parse(raw);
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("请求必须是 JSON 对象");
  }
  return value;
}

async function importChunk(distDir, prefix) {
  const names = (await readdir(distDir))
    .filter((name) => name.startsWith(`${prefix}-`) && name.endsWith(".js"))
    .sort();
  if (names.length !== 1) {
    throw new Error(`无法唯一定位 Codex 内部模块：${prefix}`);
  }
  return import(pathToFileURL(join(distDir, names[0])).href);
}

function pluginConfig(config) {
  return config?.plugins?.entries?.codex?.config || {};
}

function denyServerRequest(request) {
  if (request.method === "item/tool/call") {
    return {
      contentItems: [{
        type: "inputText",
        text: "目录会话没有注册可执行的动态工具。",
      }],
      success: false,
    };
  }
  if (
    request.method === "item/commandExecution/requestApproval"
    || request.method === "item/fileChange/requestApproval"
    || request.method.includes("requestApproval")
  ) {
    return {
      decision: "decline",
      reason: "目录会话运行在只读、无交互批准模式；外部影响动作必须回到微信确认闸门。",
    };
  }
  if (request.method === "item/permissions/requestApproval") {
    return { permissions: {}, scope: "turn" };
  }
  if (request.method === "item/tool/requestUserInput") {
    return { answers: {} };
  }
  if (request.method === "mcpServer/elicitation/request") {
    return { action: "decline" };
  }
  return {};
}

function chromeThreadConfig(config) {
  const server = config?.mcp?.servers?.node_repl;
  if (!server || typeof server !== "object" || Array.isArray(server)) {
    throw new Error("没有配置隔离的 Chrome 浏览器控制服务");
  }
  const projected = {};
  for (const key of ["command", "args", "env", "cwd"]) {
    if (server[key] !== undefined) projected[key] = server[key];
  }
  projected.default_tools_approval_mode = "auto";
  return { mcp_servers: { node_repl: projected } };
}

function threadParams(request, config) {
  return {
    cwd: requiredString(request.cwd, "cwd"),
    model: requiredString(request.model, "model"),
    modelProvider: "openai",
    personality: "none",
    approvalPolicy: "on-request",
    approvalsReviewer: "user",
    sandbox: "read-only",
    developerInstructions: [
      "你由 session-chat 目录路由层启动。",
      "只处理当前目录和当前消息；使用中文简洁回复。",
      "不得主动发送消息或执行任何外部副作用。",
      "X/Twitter 只允许复用运行时明确提供的、用户已登录的同一个 Chrome；",
      "若该 Chrome 工具不可用，必须明确失败关闭，不得切换浏览器、API 或自动登录。",
      `Chrome 浏览器客户端固定为：${requiredString(request.chrome_browser_client, "chrome_browser_client")}`,
    ].join("\n"),
    runtimeWorkspaceRoots: [requiredString(request.root, "root")],
    environments: [],
    dynamicTools: [],
    config: chromeThreadConfig(config),
    experimentalRawEvents: true,
    ephemeral: false,
  };
}

function resumeParams(request, config) {
  return {
    threadId: requiredString(request.session_id, "session_id"),
    excludeTurns: true,
    model: requiredString(request.model, "model"),
    modelProvider: "openai",
    personality: "none",
    approvalPolicy: "on-request",
    approvalsReviewer: "user",
    sandbox: "read-only",
    config: chromeThreadConfig(config),
  };
}

function nodeReplReady(status) {
  const entries = Array.isArray(status?.data) ? status.data : [];
  const server = entries.find((entry) => entry?.name === "node_repl");
  return Boolean(server?.tools && Object.hasOwn(server.tools, "js"));
}

function assertThread(response, expectedId, expectedCwd) {
  const thread = response?.thread;
  const id = requiredString(thread?.id, "thread.id");
  if (expectedId && id !== expectedId) {
    throw new Error("Codex 恢复了错误的 thread ID");
  }
  const cwd = resolve(requiredString(thread?.cwd ?? expectedCwd, "thread.cwd"));
  if (cwd !== resolve(expectedCwd)) {
    throw new Error("Codex thread 的 cwd 与目标目录不一致");
  }
  return { session_id: id, cwd };
}

export function createTurnCollector(threadId, timeoutMs) {
  let turnId = null;
  let done = false;
  let resolveDone;
  let rejectDone;
  const pending = [];
  const textByItem = new Map();
  const order = [];
  const completion = new Promise((resolvePromise, rejectPromise) => {
    resolveDone = resolvePromise;
    rejectDone = rejectPromise;
  });
  const timer = setTimeout(() => {
    if (!done) {
      done = true;
      rejectDone(new Error("Codex turn 等待超时，结果未知"));
    }
  }, timeoutMs);

  function remember(item) {
    if (!item || item.type !== "agentMessage") return;
    const id = typeof item.id === "string" ? item.id : `assistant-${order.length + 1}`;
    if (!order.includes(id)) order.push(id);
    if (typeof item.text === "string" && item.text) textByItem.set(id, item.text);
  }

  function process(notification) {
    const params = notification?.params;
    if (!params || params.threadId !== threadId) return;
    const notificationTurnId = params.turnId ?? params.turn?.id;
    if (turnId && notificationTurnId && notificationTurnId !== turnId) return;
    if (notification.method === "item/completed") {
      remember(params.item);
      return;
    }
    if (notification.method !== "turn/completed" || !turnId) return;
    for (const item of params.turn?.items || []) remember(item);
    done = true;
    clearTimeout(timer);
    const status = params.turn?.status;
    const reply = order
      .map((id) => textByItem.get(id)?.trim())
      .filter(Boolean)
      .at(-1) || "";
    if (status === "completed") {
      resolveDone({ status: "accepted_completed", reply: reply || null });
    } else if (status === "failed") {
      resolveDone({
        status: "accepted_failed",
        reply: reply || null,
        error: params.turn?.error?.message || "Codex turn 失败",
      });
    } else {
      resolveDone({
        status: "accepted_unknown",
        reply: null,
        error:
          status === "interrupted"
            ? "Codex turn 被中断，结果未知"
            : `Codex turn 返回未知状态：${String(status)}`,
      });
    }
  }

  return {
    handle(notification) {
      if (!turnId) pending.push(notification);
      else process(notification);
    },
    setTurnId(value) {
      turnId = value;
      for (const notification of pending.splice(0)) process(notification);
    },
    completion,
  };
}

async function main() {
  const request = await readRequest();
  const pluginRoot = requiredString(request.codex_plugin_root, "codex_plugin_root");
  const distDir = join(pluginRoot, "dist");
  const config = JSON.parse(
    await readFile(requiredString(request.openclaw_config, "openclaw_config"), "utf8"),
  );
  const runtimeModule = await importChunk(distDir, "config");
  const clientModule = await importChunk(distDir, "shared-client");
  const runtime = runtimeModule.d({
    pluginConfig: pluginConfig(config),
    config,
    modelProvider: "openai",
    model: request.model,
    agentDir: request.openclaw_agent_dir,
  });
  const client = await clientModule.a({
    startOptions: runtime.start,
    timeoutMs: runtime.requestTimeoutMs,
    config,
    agentDir: requiredString(request.openclaw_agent_dir, "openclaw_agent_dir"),
    isolated: true,
  });
  let notificationCleanup = () => {};
  let requestCleanup = () => {};
  try {
    requestCleanup = client.addRequestHandler(async (serverRequest) => (
      denyServerRequest(serverRequest)
    ));
    if (request.op === "probe") {
      const account = await client.request("account/read", { refreshToken: false });
      const status = await client.request("mcpServerStatus/list", { limit: 100 });
      return {
        provider_admitted: Boolean(account?.account),
        account_type: account?.account?.type || null,
        mcp_status: (Array.isArray(status?.data) ? status.data : []).map((entry) => ({
          name: entry?.name || null,
          tools: entry?.tools && typeof entry.tools === "object"
            ? Object.keys(entry.tools)
            : [],
        })),
        node_repl_ready: nodeReplReady(status),
        same_chrome_read_access: nodeReplReady(status),
      };
    }
    if (request.op === "create") {
      const response = await client.request("thread/start", threadParams(request, config));
      const verified = assertThread(response, null, request.cwd);
      // Codex 0.144.x 的纯 thread/start 只建立内存对象。写入不含用户正文的
      // 固定名称，促使官方控制面提交空 thread；不启动 turn、不调用模型。
      await client.request("thread/name/set", {
        threadId: verified.session_id,
        name: "session-chat",
      });
      return verified;
    }
    if (request.op === "resume") {
      const response = await client.request("thread/resume", resumeParams(request, config));
      return assertThread(response, request.session_id, request.cwd);
    }
    if (request.op !== "turn") throw new Error("不支持的 Codex provider 操作");

    const sessionId = requiredString(request.session_id, "session_id");
    const resumed = await client.request("thread/resume", resumeParams(request, config));
    assertThread(resumed, sessionId, request.cwd);
    const collector = createTurnCollector(sessionId, request.turn_timeout_ms);
    notificationCleanup = client.addNotificationHandler((notification) => {
      collector.handle(notification);
    });
    const started = await client.request("turn/start", {
      threadId: sessionId,
      input: [
        { type: "text", text: requiredString(request.body, "body"), text_elements: [] },
        {
          type: "skill",
          name: "x-twitter-chrome",
          path: requiredString(request.required_codex_skill, "required_codex_skill"),
        },
        {
          type: "skill",
          name: "control-chrome",
          path: requiredString(request.chrome_codex_skill, "chrome_codex_skill"),
        },
      ],
      cwd: requiredString(request.cwd, "cwd"),
      model: requiredString(request.model, "model"),
      clientUserMessageId: requiredString(request.idempotency_key, "idempotency_key"),
      approvalPolicy: "on-request",
      approvalsReviewer: "user",
      environments: [],
      runtimeWorkspaceRoots: [requiredString(request.root, "root")],
    });
    collector.setTurnId(requiredString(started?.turn?.id, "turn.id"));
    return await collector.completion;
  } finally {
    notificationCleanup();
    requestCleanup();
    clientModule.o(client);
    client.close();
  }
}

const invokedAsScript =
  process.argv[1] &&
  import.meta.url === pathToFileURL(resolve(process.argv[1])).href;

if (invokedAsScript) {
  try {
    const result = await main();
    process.stdout.write(`${JSON.stringify({ ok: true, result })}\n`);
  } catch (error) {
    process.stdout.write(`${JSON.stringify({
      ok: false,
      error: error instanceof Error ? error.message : String(error),
    })}\n`);
    process.exitCode = 2;
  }
}
