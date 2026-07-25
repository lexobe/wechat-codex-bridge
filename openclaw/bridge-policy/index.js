import { BridgePolicy, PolicyStore } from "./policy.js";

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
    const policy = new BridgePolicy({
      store,
      channelId: config.channelId || "openclaw-weixin",
      authorizedRecipients: config.authorizedRecipients || [],
      approvalTimeoutMs: config.approvalTimeoutMs || 300_000,
    });

    api.on("before_dispatch", (event, ctx) => policy.beforeDispatch(event, ctx));
    api.on("message_received", (event, ctx) => policy.observeInbound(event, ctx));
    api.on("before_tool_call", (event, ctx) => policy.beforeToolCall(event, ctx));
    api.on("message_sending", (event, ctx) => policy.messageSending(event, ctx));
    api.on("message_sent", (event, ctx) => policy.messageSent(event, ctx));
    api.on("gateway_stop", () => store.close());
  },
};
