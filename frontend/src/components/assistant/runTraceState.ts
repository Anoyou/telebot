/** Run 事件 → 轨迹摘要 / 列表的纯函数 reducer。 */

export type TraceEvent = {
  type?: string;
  seq?: number;
  ts?: string;
  created_at?: string | null;
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
  hint?: string | { web_path?: string; message?: string };
  domains?: unknown;
  skill?: unknown;
  skills?: unknown;
  skill_names?: unknown;
  understanding_summary?: string;
  route_source?: string;
  route_reason?: string;
  arguments_summary?: unknown;
  result_summary?: unknown;
  action?: unknown;
  stage_timings?: unknown;
  usage?: unknown;
  is_error?: boolean;
  [key: string]: unknown;
};

export type TraceEventCategory = "lifecycle" | "model" | "routing" | "tool" | "response" | "issue" | "action";
export type TraceEventFilter = "all" | "model" | "tool" | "issue";

export type AgentTraceOverview = {
  status: "running" | "succeeded" | "failed";
  providerName: string | null;
  model: string | null;
  domains: string[];
  skills: string[];
  toolCount: number;
  availableTools: number | null;
  retryCount: number;
  fallbackCount: number;
  inputTokens: number | null;
  outputTokens: number | null;
  totalTokens: number | null;
  verifyMs: number | null;
  routeMs: number | null;
  firstTokenMs: number | null;
  totalMs: number | null;
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

const PERSPECTIVE_HIDDEN_TYPES = new Set([
  "heartbeat",
  "assistant_delta",
  "assistant_reasoning_delta",
]);

const MODEL_EVENT_TYPES = new Set([
  "model_capability_check",
  "provider_selected",
  "model_attempt",
  "retry_scheduled",
  "model_exhausted",
]);

const ROUTING_EVENT_TYPES = new Set(["route_selected", "skill_selected"]);

export function filterTraceEvents(events: TraceEvent[]): TraceEvent[] {
  return events.filter((e) => VISIBLE_TYPES.has(String(e.type || "")));
}

export function summarizeTraceEvents(events: TraceEvent[]): TraceSummary {
  const visible = filterTraceEvents(events);
  const toolIds = new Set<string>();
  let scheduledRetryCount = 0;
  let attemptRetryCount = 0;
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
      scheduledRetryCount += 1;
    }
    if (type === "model_attempt" && Number(event.attempt || 1) > 1) {
      attemptRetryCount += 1;
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
  const retryCount = scheduledRetryCount || attemptRetryCount;
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

export function perspectiveTraceEvents(events: TraceEvent[]): TraceEvent[] {
  return events.filter((event) => !isPerspectiveNoiseEvent(event));
}

export function isPerspectiveNoiseEvent(event: TraceEvent): boolean {
  return PERSPECTIVE_HIDDEN_TYPES.has(String(event.type || ""));
}

export function traceEventCategory(event: TraceEvent): TraceEventCategory {
  const type = String(event.type || "");
  if (type === "error" || type === "model_exhausted" || type === "retry_scheduled") return "issue";
  if (type === "tool_finished" && event.is_error) return "issue";
  if (type === "done" && event.ok === false) return "issue";
  if (MODEL_EVENT_TYPES.has(type)) return "model";
  if (ROUTING_EVENT_TYPES.has(type)) return "routing";
  if (type === "tool_started" || type === "tool_finished") return "tool";
  if (type === "assistant_message" || type === "assistant_delta_reset" || type === "done") return "response";
  if (type === "action_proposed") return "action";
  return "lifecycle";
}

export function traceEventHint(value: TraceEvent["hint"] | unknown): string {
  if (typeof value === "string") return value;
  const record = asRecord(value);
  if (!record) return "";
  return [record.message, record.web_path]
    .filter((item): item is string => typeof item === "string" && Boolean(item.trim()))
    .join(" · ");
}

export function filterPerspectiveEvents(
  events: TraceEvent[],
  filter: TraceEventFilter,
): TraceEvent[] {
  const visible = perspectiveTraceEvents(events);
  if (filter === "all") return visible;
  return visible.filter((event) => {
    const category = traceEventCategory(event);
    if (filter === "model") {
      return category === "model" || category === "routing" || MODEL_EVENT_TYPES.has(String(event.type || ""));
    }
    if (filter === "tool") {
      return category === "tool" || category === "action" || ["tool_started", "tool_finished"].includes(String(event.type || ""));
    }
    return category === "issue";
  });
}

export function defaultPerspectiveEvent(events: TraceEvent[]): TraceEvent | null {
  const visible = perspectiveTraceEvents(events);
  const problem = [...visible].reverse().find((event) => (
    traceEventCategory(event) === "issue" && event.type !== "done"
  ));
  return problem ?? visible.at(-1) ?? null;
}

export function buildAgentTraceOverview(events: TraceEvent[]): AgentTraceOverview {
  const summary = summarizeTraceEvents(events);
  const done = [...events].reverse().find((event) => event.type === "done");
  const assistant = [...events].reverse().find((event) => event.type === "assistant_message");
  const provider = [...events].reverse().find((event) => event.type === "provider_selected");
  const route = [...events].reverse().find((event) => event.type === "route_selected");
  const skill = [...events].reverse().find((event) => event.type === "skill_selected");
  const usage = asRecord(assistant?.usage);
  const timing = asRecord(usage?.stage_timings) ?? asRecord(done?.stage_timings);
  const usedFallback = usage?.used_fallback === true || done?.used_fallback === true;

  return {
    status: summary.failed || done?.ok === false ? "failed" : done?.ok === true ? "succeeded" : "running",
    providerName: stringValue(usage?.provider_name) ?? stringValue(provider?.provider_name),
    model: stringValue(usage?.model) ?? stringValue(provider?.model),
    domains: stringArray(route?.domains ?? usage?.route_domains),
    skills: stringArray(skill?.skill_names ?? skill?.skills ?? skill?.skill),
    toolCount: Math.max(summary.toolCount, numberValue(usage?.tool_calls ?? done?.tool_calls) ?? 0),
    availableTools: numberValue(usage?.available_tools ?? done?.available_tools ?? route?.tool_count),
    retryCount: summary.retryCount,
    fallbackCount: Math.max(summary.fallbackCount, usedFallback ? 1 : 0),
    inputTokens: numberValue(usage?.input_tokens),
    outputTokens: numberValue(usage?.output_tokens),
    totalTokens: numberValue(usage?.total_tokens),
    verifyMs: numberValue(timing?.verify_ms),
    routeMs: numberValue(timing?.route_ms),
    firstTokenMs: numberValue(timing?.first_token_ms),
    totalMs: numberValue(timing?.total_ms ?? usage?.elapsed_ms),
  };
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function stringValue(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function numberValue(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim()) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
  }
  return null;
}

function stringArray(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map((item) => String(item || "").trim()).filter(Boolean);
  }
  const single = stringValue(value);
  return single ? [single] : [];
}
