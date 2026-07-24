import type { SystemAgentMessage } from "@/api/systemAgent";

/** 流式中未闭合的 ``` 围栏临时补闭合，避免后续正文被吞进代码块。 */
export function stabilizeStreamingMarkdown(text: string): string {
  const fenceCount = (text.match(/^```/gm) || []).length;
  if (fenceCount % 2 === 1) return `${text}\n\`\`\``;
  return text;
}

/** Tool messages are runtime evidence, not a second conversational reply. */
export function visibleConversationMessages(
  messages: SystemAgentMessage[],
): SystemAgentMessage[] {
  return messages.filter((message) => message.role !== "tool");
}

/** 命名色 → class；十六进制 / rgb 走安全 style=color。 */
export const SAFE_TEXT_COLORS: Record<string, string> = {
  red: "assistant-html-text-red",
  blue: "assistant-html-text-blue",
  green: "assistant-html-text-green",
  yellow: "assistant-html-text-yellow",
  purple: "assistant-html-text-purple",
  violet: "assistant-html-text-purple",
  gray: "assistant-html-text-gray",
  grey: "assistant-html-text-gray",
  orange: "assistant-html-text-orange",
  gold: "assistant-html-text-gold",
  golden: "assistant-html-text-gold",
  pink: "assistant-html-text-pink",
  cyan: "assistant-html-text-cyan",
  teal: "assistant-html-text-teal",
  magenta: "assistant-html-text-magenta",
  indigo: "assistant-html-text-indigo",
  brown: "assistant-html-text-brown",
  black: "assistant-html-text-black",
  white: "assistant-html-text-white",
  橙: "assistant-html-text-orange",
  橙色: "assistant-html-text-orange",
  金: "assistant-html-text-gold",
  金色: "assistant-html-text-gold",
  粉: "assistant-html-text-pink",
  粉色: "assistant-html-text-pink",
  粉红: "assistant-html-text-pink",
  红: "assistant-html-text-red",
  红色: "assistant-html-text-red",
  绿: "assistant-html-text-green",
  绿色: "assistant-html-text-green",
  蓝: "assistant-html-text-blue",
  蓝色: "assistant-html-text-blue",
  紫: "assistant-html-text-purple",
  紫色: "assistant-html-text-purple",
  黄: "assistant-html-text-yellow",
  黄色: "assistant-html-text-yellow",
  灰: "assistant-html-text-gray",
  灰色: "assistant-html-text-gray",
};

export const SAFE_LABELS: Record<string, string> = {
  success: "assistant-html-label-success",
  warn: "assistant-html-label-warn",
  warning: "assistant-html-label-warn",
  danger: "assistant-html-label-danger",
  info: "assistant-html-label-info",
  neutral: "assistant-html-label-neutral",
};

export const ASSISTANT_HTML_CLASSES = [
  ...new Set([...Object.values(SAFE_TEXT_COLORS), ...Object.values(SAFE_LABELS)]),
];

const SAFE_HEX_COLOR_RE = /^#(?:[0-9a-f]{3}|[0-9a-f]{6}|[0-9a-f]{8})$/i;
const SAFE_RGB_COLOR_RE =
  /^rgba?\(\s*(?:\d{1,3}%?\s*,\s*){2}\d{1,3}%?(?:\s*,\s*(?:0|1|0?\.\d+|1\.0+))?\s*\)$/i;

/** 从 style 字符串取出 color 值。 */
export function extractStyleColor(style: string): string | null {
  const match = /(?:^|;)\s*color\s*:\s*([^;]+?)\s*(?:;|$)/i.exec(String(style || ""));
  return match ? match[1].trim() : null;
}

/** 解析命名色 / 十六进制 / rgb 为 class 或安全 style。 */
export function resolveAssistantTextColor(raw: string): { className?: string; style?: string } {
  const value = String(raw || "").trim();
  if (!value) return {};
  const lower = value.toLowerCase();
  const named = SAFE_TEXT_COLORS[lower] || SAFE_TEXT_COLORS[value];
  if (named) return { className: named };
  const compact = lower.replace(/\s+/g, "");
  if (SAFE_HEX_COLOR_RE.test(compact)) return { style: `color: ${compact}` };
  if (SAFE_RGB_COLOR_RE.test(lower)) return { style: `color: ${lower}` };
  return {};
}
