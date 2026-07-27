import { spawn } from "node:child_process";
import { delimiter } from "node:path";
import { createInterface } from "node:readline";

export class SessionChatClient {
  constructor({
    pythonPath = "python3",
    projectRoot,
    root,
    dbPath,
    providerMode = "disabled",
    requiredCodexSkill,
    chromeCodexSkill,
    chromeBrowserClient,
    codexRunner,
    codexPluginRoot,
    openclawConfig,
    openclawAgentDir,
    nodePath = "node",
    model = "gpt-5.6-sol",
    requestTimeoutMs = 30_000,
    spawnProcess = spawn,
  }) {
    this.pythonPath = pythonPath;
    this.projectRoot = projectRoot;
    this.root = root;
    this.dbPath = dbPath;
    this.providerMode = providerMode;
    this.requiredCodexSkill = requiredCodexSkill;
    this.chromeCodexSkill = chromeCodexSkill;
    this.chromeBrowserClient = chromeBrowserClient;
    this.codexRunner = codexRunner;
    this.codexPluginRoot = codexPluginRoot;
    this.openclawConfig = openclawConfig;
    this.openclawAgentDir = openclawAgentDir;
    this.nodePath = nodePath;
    this.model = model;
    this.requestTimeoutMs = requestTimeoutMs;
    this.spawnProcess = spawnProcess;
    this.child = null;
    this.nextId = 1;
    this.pending = new Map();
  }

  start() {
    if (this.child) return;
    const args = [
      "-m",
      "wechat_codex_bridge.session_runtime",
      "serve",
      "--root",
      this.root,
      "--db",
      this.dbPath,
      "--provider-mode",
      this.providerMode,
      "--required-codex-skill",
      this.requiredCodexSkill,
      "--chrome-codex-skill",
      this.chromeCodexSkill,
      "--chrome-browser-client",
      this.chromeBrowserClient,
      "--node-path",
      this.nodePath,
      "--model",
      this.model,
    ];
    if (this.providerMode === "codex") {
      args.push(
        "--codex-runner",
        this.codexRunner,
        "--codex-plugin-root",
        this.codexPluginRoot,
        "--openclaw-config",
        this.openclawConfig,
        "--openclaw-agent-dir",
        this.openclawAgentDir,
      );
    }
    const pythonPath = `${this.projectRoot}/src`;
    const existing = process.env.PYTHONPATH;
    this.child = this.spawnProcess(this.pythonPath, args, {
      cwd: this.projectRoot,
      env: {
        ...process.env,
        PYTHONPATH: existing
          ? `${pythonPath}${delimiter}${existing}`
          : pythonPath,
      },
      stdio: ["pipe", "pipe", "pipe"],
    });
    const lines = createInterface({ input: this.child.stdout });
    lines.on("line", (line) => this.handleLine(line));
    this.child.stderr.on("data", (chunk) => {
      process.stderr.write(`[session-chat] ${String(chunk)}`);
    });
    this.child.once("exit", (code, signal) => {
      const reason = new Error(
        `session-chat 运行时已退出（code=${code}, signal=${signal}）`,
      );
      this.rejectAll(reason);
      this.child = null;
    });
    this.child.once("error", (error) => {
      this.rejectAll(error);
      this.child = null;
    });
  }

  handleLine(line) {
    let response;
    try {
      response = JSON.parse(line);
    } catch {
      this.rejectAll(new Error("session-chat 返回了无效 JSON"));
      return;
    }
    const entry = this.pending.get(response.id);
    if (!entry) return;
    this.pending.delete(response.id);
    clearTimeout(entry.timer);
    if (response.ok === true) {
      entry.resolve(response.result);
      return;
    }
    const error = new Error(response.error?.message || "session-chat 请求失败");
    error.code = response.error?.code || "SessionChatError";
    entry.reject(error);
  }

  rejectAll(error) {
    for (const entry of this.pending.values()) {
      clearTimeout(entry.timer);
      entry.reject(error);
    }
    this.pending.clear();
  }

  request(payload) {
    this.start();
    if (!this.child?.stdin?.writable) {
      return Promise.reject(new Error("session-chat 运行时不可写"));
    }
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error("session-chat 请求超时，已故障关闭"));
      }, this.requestTimeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      this.child.stdin.write(`${JSON.stringify({ id, ...payload })}\n`);
    });
  }

  route(inbound) {
    return this.request({ op: "route", ...inbound });
  }

  probe() {
    return this.request({ op: "probe" });
  }

  close() {
    if (!this.child) return;
    this.rejectAll(new Error("session-chat 运行时正在停止"));
    this.child.kill("SIGTERM");
    this.child = null;
  }
}
