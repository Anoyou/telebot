const TOOL_TERM_REPLACEMENTS: Array<[RegExp, string]> = [
  [/Scheduler 定时任务/gi, "定时任务"],
  [/\bScheduler\b/gi, "定时任务"],
  [/\bProvider\b/gi, "模型提供商"],
  [/\bRule\b/g, "规则"],
  [/routing_mode/g, "路由模式"],
  [/\bfixed\b/g, "固定"],
  [/\bauto\b/g, "自动"],
];

export function systemAgentToolLabel(
  description?: string | null,
  fallback = "系统能力",
): string {
  const concise = description?.trim().split(/[（(，,。；;：:]/, 1)[0]?.trim();
  if (!concise) return fallback;
  const translated = TOOL_TERM_REPLACEMENTS.reduce(
    (label, [pattern, replacement]) => label.replace(pattern, replacement),
    concise,
  );
  return translated.replace(/([\u3400-\u9fff])\s+(?=[\u3400-\u9fff])/g, "$1");
}
