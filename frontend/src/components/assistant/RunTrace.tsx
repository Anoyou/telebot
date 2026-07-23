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
import { cn } from "@/lib/utils";

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
    case "assistant_message":
      return "最终回答已生成";
    case "error":
      return `错误 · ${event.code || ""} ${event.message || ""}`.trim();
    case "done":
      return event.ok ? "完成" : "结束（未成功）";
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
}: {
  runId?: string | null;
  /** 当前流式轮次的实时事件（可选） */
  liveEvents?: SystemAgentStreamEvent[];
  running?: boolean;
  failed?: boolean;
  className?: string;
  defaultOpen?: boolean;
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

  const visible = useMemo(() => filterTraceEvents(events), [events]);
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
          <ol className="max-h-48 space-y-1 overflow-y-auto rounded-md border bg-muted/20 px-2 py-1.5">
            {visible.map((event, index) => (
              <li key={`${event.seq ?? index}-${event.type}`} className="text-[11px] leading-4">
                <span className="tabular-nums text-muted-foreground/80">
                  {typeof event.seq === "number" ? `#${event.seq} ` : ""}
                </span>
                {labelEvent(event)}
              </li>
            ))}
          </ol>
        )
      ) : null}
    </div>
  );
}
