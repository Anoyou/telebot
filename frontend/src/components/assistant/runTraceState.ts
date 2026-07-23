/** Run 事件 → 轨迹摘要 / 列表的纯函数 reducer。 */

export type TraceEvent = {
  type?: string;
  seq?: number;
  call_id?: string;
  tool_name?: string;
  tool_description?: string;
  provider_name?: string;
  model?: string;
  reason?: string;
  attempt?: number;
  retry_number?: number;
  max_retries?: number;
  ok?: boolean;
  code?: string;
  message?: string;
  domains?: unknown;
  skill?: unknown;
  skills?: unknown;
  is_error?: boolean;
  [key: string]: unknown;
};

export type TraceSummary = {
  toolCount: number;
  retryCount: number;
  fallbackCount: number;
  failed: boolean;
  errorMessage: string | null;
  /** 人类可读摘要，如「已完成 · 2 个工具 · 1 次重试」 */
  headline: string;
};

const VISIBLE_TYPES = new Set([
  "route_selected",
  "skill_selected",
  "tool_started",
  "tool_finished",
  "provider_selected",
  "model_attempt",
  "retry_scheduled",
  "model_exhausted",
  "model_capability_check",
  "assistant_delta_reset",
  "assistant_message",
  "error",
  "done",
]);

export function filterTraceEvents(events: TraceEvent[]): TraceEvent[] {
  return events.filter((e) => VISIBLE_TYPES.has(String(e.type || "")));
}

export function summarizeTraceEvents(events: TraceEvent[]): TraceSummary {
  const visible = filterTraceEvents(events);
  const toolIds = new Set<string>();
  let retryCount = 0;
  let fallbackCount = 0;
  let failed = false;
  let errorMessage: string | null = null;

  for (const event of visible) {
    const type = String(event.type || "");
    if (type === "tool_started") {
      const id = String(event.call_id || event.tool_name || `${event.seq}`);
      toolIds.add(id);
    }
    if (type === "retry_scheduled") {
      retryCount += 1;
    }
    if (type === "model_attempt" && Number(event.attempt || 1) > 1) {
      retryCount += 1;
    }
    if (type === "provider_selected") {
      const reason = String(event.reason || "");
      if (reason.includes("fallback")) fallbackCount += 1;
    }
    if (type === "error") {
      failed = true;
      errorMessage = [event.code, event.message].filter(Boolean).join(" ") || "执行失败";
    }
    if (type === "done" && event.ok === false) {
      failed = true;
    }
  }

  const toolCount = toolIds.size;
  if (failed) {
    return {
      toolCount,
      retryCount,
      fallbackCount,
      failed: true,
      errorMessage,
      headline: errorMessage
        ? `执行失败 · ${errorMessage}`
        : "执行失败 · 可重试或切换 Provider",
    };
  }

  const parts = ["已完成"];
  if (toolCount > 0) parts.push(`${toolCount} 个工具`);
  if (retryCount > 0) parts.push(`${retryCount} 次重试`);
  if (fallbackCount > 0) parts.push(`${fallbackCount} 次切换`);
  return {
    toolCount,
    retryCount,
    fallbackCount,
    failed: false,
    errorMessage: null,
    headline: parts.join(" · "),
  };
}
