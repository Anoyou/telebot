export type PluginEventSubscription = Record<string, unknown>;
export type PluginCapabilities = Record<string, unknown>;

const HIGH_RISK_TERMS = [
  "telegram_native_raw",
  "native_raw",
  "inline_all",
  "bbot_notice",
  "notice_bot",
];
const DEPRECATED_SEND_CHANNELS = ["notice", "bbot_notice", "notice_bot"];
const AI_PERMISSION_TERMS = ["ai", "ai_text", "llm", "llm_text", "openai", "anthropic"];

const EVENT_LABELS: Record<string, string> = {
  all_messages: "全部消息",
  callback_query: "按钮回调",
  chosen_inline_result: "Inline 选择",
  command: "命令",
  external_payment_notice: "外部付款通知",
  inline_query: "Inline 查询",
  keyword: "关键词",
  message: "普通消息",
  payment_confirmed: "付款确认",
  session_close: "会话关闭",
};

const CAPABILITY_LABELS: Record<string, string> = {
  answer_callback: "按钮 ACK",
  answer_inline_query: "Inline 回答",
  ai: "AI 调用",
  ai_text: "AI 文本",
  llm: "AI 调用",
  llm_text: "AI 文本",
  inline_all: "Inline 全量",
  settlement: "结算动作",
  telegram_direct_passthrough: "裸直通",
  telegram_native_raw: "native_raw",
  native_raw: "native_raw",
  userbot_reply: "人形回复",
};

const PERMISSION_LABELS: Record<string, string> = {
  ai: "AI 调用",
  ai_text: "AI 文本",
  edit_message: "编辑消息",
  external_http: "HTTP 请求",
  read_chat: "读取消息",
  resolve_entity: "解析会话",
  send_file: "发送文件",
  send_message: "发送消息",
};

export function pluginEventSubscriptionLabels(
  subscriptions?: PluginEventSubscription[] | null,
): string[] {
  const labels = new Set<string>();
  for (const subscription of subscriptions ?? []) {
    const rawEvents = firstArrayValue(subscription, ["event_types", "events", "types"]);
    for (const event of rawEvents) {
      if (typeof event === "string" && event.trim()) {
        labels.add(EVENT_LABELS[event] || event);
      }
    }
    const rawType = subscription.event_type ?? subscription.type ?? subscription.event;
    if (typeof rawType === "string" && rawType.trim()) {
      labels.add(EVENT_LABELS[rawType] || rawType);
    }
    const source = subscription.source_channel ?? subscription.source;
    if (typeof source === "string" && source.trim()) {
      labels.add(source);
    }
  }
  return [...labels];
}

export function pluginCapabilityLabels(capabilities?: PluginCapabilities | null): string[] {
  const labels = new Set<string>();
  for (const [key, value] of Object.entries(capabilities ?? {})) {
    if (key === "telegram_direct_passthrough" && !pluginSupportsDirectPassthrough(capabilities)) continue;
    if (!isCapabilityEnabled(value)) continue;
    labels.add(CAPABILITY_LABELS[key] || key);
  }
  return [...labels];
}

export function pluginOperationalCapabilityLabels(input: {
  capabilities?: PluginCapabilities | null;
  permissions?: string[] | null;
  config_schema?: Record<string, unknown> | null;
  usage?: unknown;
  description?: unknown;
}): string[] {
  const labels = new Set(pluginCapabilityLabels(input.capabilities));
  for (const permission of input.permissions ?? []) {
    const key = String(permission || "").trim();
    if (!key) continue;
    labels.add(PERMISSION_LABELS[key] || key);
  }
  if (pluginUsesAI(input)) labels.add("AI 调用");
  return [...labels];
}

export function pluginUsesAI(input: {
  capabilities?: PluginCapabilities | null;
  permissions?: string[] | null;
  config_schema?: Record<string, unknown> | null;
  usage?: unknown;
  description?: unknown;
}): boolean {
  const permissions = (input.permissions ?? []).map((item) => String(item || "").trim().toLowerCase());
  if (permissions.some((item) => AI_PERMISSION_TERMS.includes(item) || item.startsWith("ai_") || item.startsWith("llm_"))) {
    return true;
  }
  const capabilitiesText = JSON.stringify(input.capabilities ?? {}).toLowerCase();
  if (AI_PERMISSION_TERMS.some((term) => capabilitiesText.includes(term))) return true;
  const schemaText = JSON.stringify(input.config_schema ?? {}).toLowerCase();
  if (
    schemaText.includes("llm") ||
    schemaText.includes("ai_provider") ||
    schemaText.includes("llm_provider") ||
    schemaText.includes("provider_tag") ||
    schemaText.includes("ai_usage_guide")
  ) return true;
  const prose = `${String(input.usage ?? "")} ${String(input.description ?? "")}`.toLowerCase();
  return prose.includes("ctx.ai") || prose.includes("llm") || prose.includes("ai provider") || prose.includes("ai 调用");
}

export function pluginSupportsDirectPassthrough(capabilities?: PluginCapabilities | null): boolean {
  const value = capabilities?.telegram_direct_passthrough;
  if (value === true) return true;
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  return (value as Record<string, unknown>).enabled === true;
}

export function pluginContractRiskWarnings(input: {
  capabilities?: PluginCapabilities | null;
  event_subscriptions?: PluginEventSubscription[] | null;
  lint_warnings?: string[] | null;
}): string[] {
  const haystack = JSON.stringify({
    capabilities: input.capabilities ?? {},
    event_subscriptions: input.event_subscriptions ?? [],
    lint_warnings: input.lint_warnings ?? [],
  }).toLowerCase();
  const warnings: string[] = [];
  if (pluginSupportsDirectPassthrough(input.capabilities)) {
    warnings.push("高风险：插件声明裸直通，会在标准 Event Bus / Trace / MessageOps 链路前收到原始消息。");
  }
  if (haystack.includes("telegram_native_raw") || haystack.includes("native_raw")) {
    warnings.push("高风险：插件声明 native_raw，可读取 Telegram 原生事件摘要；仅安装可信来源。");
  }
  if (haystack.includes("inline_all")) {
    warnings.push("高风险：插件声明 inline_all，可能处理全部 Inline 查询。");
  }
  for (const channel of DEPRECATED_SEND_CHANNELS) {
    if (containsDeprecatedSendChannel({
      capabilities: input.capabilities ?? {},
      event_subscriptions: input.event_subscriptions ?? [],
      lint_warnings: input.lint_warnings ?? [],
    }, channel)) {
      warnings.push(`高风险：检测到废弃通道 ${channel}，最终版应改用 MessageOps/action。`);
    }
  }
  return [...new Set(warnings)];
}

export function pluginHasHighRiskContract(input: {
  capabilities?: PluginCapabilities | null;
  event_subscriptions?: PluginEventSubscription[] | null;
  lint_warnings?: string[] | null;
}): boolean {
  const haystack = JSON.stringify(input).toLowerCase();
  return pluginSupportsDirectPassthrough(input.capabilities) ||
    HIGH_RISK_TERMS.some((term) => haystack.includes(term)) ||
    DEPRECATED_SEND_CHANNELS.some((channel) => containsDeprecatedSendChannel(input, channel));
}

export function compactUsageText(value?: unknown, fallback = "未声明 usage"): string {
  if (typeof value !== "string") return fallback;
  const text = value.trim().replace(/\s+/g, " ");
  if (!text) return fallback;
  return text.length > 120 ? `${text.slice(0, 120)}...` : text;
}

function firstArrayValue(source: PluginEventSubscription, keys: string[]): unknown[] {
  for (const key of keys) {
    const value = source[key];
    if (Array.isArray(value)) return value;
  }
  return [];
}

function isCapabilityEnabled(value: unknown): boolean {
  if (value === false || value == null) return false;
  if (Array.isArray(value)) return value.length > 0;
  if (typeof value === "object") return Object.keys(value as Record<string, unknown>).length > 0;
  return true;
}

function containsDeprecatedSendChannel(source: unknown, channel: string): boolean {
  if (typeof source === "string") {
    if (source === channel) return true;
    return new RegExp(`(^|[^a-z_])${channel}([^a-z_]|$)`, "i").test(source);
  }
  if (Array.isArray(source)) return source.some((item) => containsDeprecatedSendChannel(item, channel));
  if (!source || typeof source !== "object") return false;
  return Object.values(source as Record<string, unknown>).some((value) => containsDeprecatedSendChannel(value, channel));
}
