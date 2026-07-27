import { BridgePolicy, PolicyStore } from "./policy.js";
import { SessionChatClient } from "./session-chat-client.js";

export default {
  id: "wechat-codex-bridge-policy",
  name: "微信—Codex 桥接策略",
  description: "微信单人私聊、入站幂等、精确批准和投递回执边界",
  register(api) {
    const config = api.pluginConfig || {};
    if (!config.dbPath) {
      throw new Error("wechat-codex-bridge-policy 必须配置 dbPath");
    }
    const store = new PolicyStore(config.dbPath);
    const sessionConfig = config.sessionChat || {};
    const sessionClient = sessionConfig.enabled === true
      ? new SessionChatClient({
          pythonPath: sessionConfig.pythonPath || "python3",
          projectRoot: sessionConfig.projectRoot,
          root: sessionConfig.root,
          dbPath: sessionConfig.dbPath,
          providerMode: sessionConfig.providerMode || "disabled",
          requiredCodexSkill: sessionConfig.requiredCodexSkill,
          chromeCodexSkill: sessionConfig.chromeCodexSkill,
          chromeBrowserClient: sessionConfig.chromeBrowserClient,
          codexRunner: sessionConfig.codexRunner,
          codexPluginRoot: sessionConfig.codexPluginRoot,
          openclawConfig: sessionConfig.openclawConfig,
          openclawAgentDir: sessionConfig.openclawAgentDir,
          nodePath: sessionConfig.nodePath || "node",
          model: sessionConfig.model || "gpt-5.6-sol",
          requestTimeoutMs: sessionConfig.requestTimeoutMs || 30_000,
        })
      : null;
    const policy = new BridgePolicy({
      store,
      channelId: config.channelId || "openclaw-weixin",
      authorizedRecipients: config.authorizedRecipients || [],
      approvalTimeoutMs: config.approvalTimeoutMs || 300_000,
      sessionClient,
      sendSessionReply: async ({ channel, accountId, target, body }) => {
        const adapter = await api.runtime.channel.outbound.loadAdapter(channel);
        if (!adapter?.sendText) {
          throw new Error("微信渠道没有可用的 sendText 投递接口");
        }
        return adapter.sendText({
          cfg: api.config,
          to: target,
          text: body,
          accountId,
        });
      },
    });

    api.on("before_dispatch", (event, ctx) => policy.beforeDispatch(event, ctx));
    api.on("message_received", (event, ctx) => policy.observeInbound(event, ctx));
    api.on("before_tool_call", (event, ctx) => policy.beforeToolCall(event, ctx));
    api.on("message_sending", (event, ctx) => policy.messageSending(event, ctx));
    api.on("message_sent", (event, ctx) => policy.messageSent(event, ctx));
    api.on("gateway_stop", () => {
      sessionClient?.close();
      store.close();
    });
  },
};
