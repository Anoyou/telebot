import { useEffect, useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ChevronDown, ChevronRight } from "lucide-react";

import { listSystemAgentRunEvents, type SystemAgentStreamEvent } from "@/api/systemAgent";
import {
  filterTraceEvents,
  summarizeTraceEvents,
  type TraceEvent,
} from "@/components/assistant/runTraceState";
import { Skeleton } from "@/components/ui/misc";
import { systemAgentToolLabel } from "@/lib/systemAgentLabels";
import { cn, formatDateTime } from "@/lib/utils";

function diagnosticFacts(event: TraceEvent): string[] {
  const facts: string[] = [];
  const add = (label: string, value: unknown) => {
    if (value === undefined || value === null || value === "") return;
    facts.push(`${label}=${typeof value === "object" ? JSON.stringify(value) : String(value)}`);
  };
  add("Provider", event.provider_name);
  add("model", event.model);
  add("attempt", event.attempt);
  add("retry", event.retry_number);
  add("tool", event.tool_name);
  add("call_id", event.call_id);
  add("code", event.code);
  add("usage", event.usage);
  const timings =
    event.stage_timings && typeof event.stage_timings === "object"
      ? (event.stage_timings as Record<string, unknown>)
      : event.usage &&
          typeof event.usage === "object" &&
          (event.usage as Record<string, unknown>).stage_timings &&
          typeof (event.usage as Record<string, unknown>).stage_timings === "object"
        ? ((event.usage as Record<string, unknown>).stage_timings as Record<string, unknown>)
        : null;
  if (timings) {
    for (const key of ["verify_ms", "route_ms", "first_token_ms", "total_ms"] as const) {
      const value = timings[key];
      if (value != null && value !== "") add(key, value);
    }
  }
  return facts;
}

function labelEvent(event: TraceEvent): string {
  const type = String(event.type || "");
  switch (type) {
    case "route_selected": {
      const domains = Array.isArray(event.domains) ? event.domains.join(", ") : "";
      return `路由 → ${domains || "无工具"}`;
    }
    case "skill_selected":
      return `技能 → ${String(event.skill || event.skills || "已选")}`;
    case "tool_started":
      return `工具开始 · ${systemAgentToolLabel(String(event.tool_description || ""), String(event.tool_name || ""))}`;
    case "tool_finished":
      return `工具${event.is_error ? "失败" : "完成"} · ${systemAgentToolLabel(String(event.tool_description || ""), String(event.tool_name || ""))}`;
    case "provider_selected":
      return `Provider · ${event.provider_name || "?"} · ${event.model || "?"} (${event.reason || "selected"})`;
    case "model_capability_check":
      return `能力检查 · ${event.provider_name || "?"} · ${event.model || "?"}`;
    case "model_attempt":
      return `尝试 · ${event.provider_name || "?"} · ${event.model || "?"} #${event.attempt || 1}`;
    case "retry_scheduled":
      return `重试计划 · 第 ${event.retry_number || "?"}/${event.max_retries || "?"} 次`;
    case "model_exhausted":
      return `模型耗尽 · ${event.provider_name || "?"} · ${event.model || "?"}`;
    case "assistant_delta_reset":
      return "清空流式草稿（准备调工具）";
    case "assistant_message": {
      const usage =
        event.usage && typeof event.usage === "object"
          ? (event.usage as Record<string, unknown>)
          : null;
      const timings =
        usage?.stage_timings && typeof usage.stage_timings === "object"
          ? (usage.stage_timings as Record<string, unknown>)
          : null;
      const total = timings?.total_ms;
      return total != null && total !== "" ? `最终回答已生成 · ${total}ms` : "最终回答已生成";
    }
    case "error":
      return `错误 · ${event.code || ""} ${event.message || ""}`.trim();
    case "done": {
      const timings =
        event.stage_timings && typeof event.stage_timings === "object"
          ? (event.stage_timings as Record<string, unknown>)
          : null;
      const total = timings?.total_ms;
      const base = event.ok ? "完成" : "结束（未成功）";
      return total != null && total !== "" ? `${base} · ${total}ms` : base;
    }
    default:
      return type || "事件";
  }
}

/**
 * 执行轨迹：运行中默认展开；完成自动收起为摘要；失败保持展开。
 * 历史消息：传入 runId 后点击才加载（由 open 控制 query）。
 */
export function RunTrace({
  runId,
  liveEvents,
  running,
  failed,
  className,
  defaultOpen,
  mode = "compact",
  timezone,
}: {
  runId?: string | null;
  /** 当前流式轮次的实时事件（可选） */
  liveEvents?: SystemAgentStreamEvent[];
  running?: boolean;
  failed?: boolean;
  className?: string;
  defaultOpen?: boolean;
  mode?: "compact" | "diagnostic";
  timezone?: string;
}) {
  const [open, setOpen] = useState(Boolean(defaultOpen ?? (running || failed)));

  useEffect(() => {
    if (running) setOpen(true);
    else if (failed) setOpen(true);
    else if (!running && !failed && liveEvents?.length) setOpen(false);
  }, [running, failed, liveEvents?.length]);

  const q = useQuery({
    queryKey: ["system-agent", "run-events", runId],
    queryFn: () => listSystemAgentRunEvents(runId!),
    enabled: Boolean(runId) && open && !liveEvents?.length,
    staleTime: 30_000,
  });

  const events: TraceEvent[] = useMemo(() => {
    if (liveEvents?.length) return liveEvents as TraceEvent[];
    return (q.data || []) as TraceEvent[];
  }, [liveEvents, q.data]);

  const visible = useMemo(
    () => (mode === "diagnostic" ? events : filterTraceEvents(events)),
    [events, mode],
  );
  const summary = useMemo(() => summarizeTraceEvents(events), [events]);

  if (!runId && !liveEvents?.length) return null;

  return (
    <div className={cn("space-y-1 text-[11px] leading-4", className)}>
      <button
        type="button"
        className={cn(
          "inline-flex max-w-full items-center gap-0.5 text-left text-muted-foreground hover:text-foreground",
          failed && "text-destructive",
        )}
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        {open ? <ChevronDown className="h-3 w-3 shrink-0" /> : <ChevronRight className="h-3 w-3 shrink-0" />}
        <span className="truncate">
          {running
            ? "执行中…"
            : summary.headline || (open ? "执行轨迹" : "执行轨迹（点击展开）")}
        </span>
      </button>
      {open ? (
        q.isLoading && !liveEvents?.length ? (
          <div className="rounded-md border bg-muted/20 p-2">
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="mt-2 h-4 w-1/2" />
          </div>
        ) : q.isError && !liveEvents?.length ? (
          <div className="rounded-md border border-destructive/30 bg-destructive/5 px-2 py-1.5 text-destructive">
            无法加载执行轨迹
          </div>
        ) : !visible.length ? (
          <div className="rounded-md border bg-muted/20 px-2 py-1.5 text-muted-foreground">
            暂无轨迹事件
          </div>
        ) : (
          <ol className={cn(
            "space-y-1 overflow-y-auto rounded-md border bg-muted/20 px-2 py-1.5",
            mode === "diagnostic" ? "max-h-[32rem]" : "max-h-48",
          )}>
            {visible.map((event, index) => (
              <li key={`${event.seq ?? index}-${event.type}`} className={cn("text-[11px] leading-4", mode === "diagnostic" && "border-b border-border/50 py-1.5 last:border-0")}>
                <div className="flex min-w-0 flex-wrap items-baseline gap-x-1.5">
                  {mode === "diagnostic" && event.created_at ? (
                    <span className="shrink-0 tabular-nums text-muted-foreground/80">
                      {formatDateTime(String(event.created_at), timezone)}
                    </span>
                  ) : null}
                  <span className="tabular-nums text-muted-foreground/80">
                    {typeof event.seq === "number" ? `#${event.seq}` : ""}
                  </span>
                  {mode === "diagnostic" ? <code className="text-[10px] text-info">{String(event.type || "event")}</code> : null}
                  <span>{labelEvent(event)}</span>
                </div>
                {mode === "diagnostic" && diagnosticFacts(event).length ? (
                  <div className="mt-1 break-all font-mono text-[10px] text-muted-foreground">
                    {diagnosticFacts(event).join(" · ")}
                  </div>
                ) : null}
                {mode === "diagnostic" ? (
                  <details className="mt-1 text-muted-foreground">
                    <summary className="cursor-pointer select-none text-[10px] hover:text-foreground">原始事件 JSON</summary>
                    <pre className="mt-1 max-h-56 overflow-auto whitespace-pre-wrap break-all rounded bg-background/80 p-2 text-[10px] leading-4 text-foreground">{JSON.stringify(event, null, 2)}</pre>
                  </details>
                ) : null}
              </li>
            ))}
          </ol>
        )
      ) : null}
    </div>
  );
}
