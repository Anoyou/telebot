import { useEffect, useMemo, useRef, useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ChevronRight } from "lucide-react";

import { listSystemAgentRunEvents, type SystemAgentStreamEvent } from "@/api/systemAgent";
import { AgentRunPerspective } from "@/components/assistant/AgentRunPerspective";
import {
  filterTraceEvents,
  isPerspectiveNoiseEvent,
  summarizeTraceEvents,
  type TraceEvent,
} from "@/components/assistant/runTraceState";
import { Skeleton } from "@/components/ui/misc";
import { systemAgentToolLabel } from "@/lib/systemAgentLabels";
import { cn } from "@/lib/utils";

const RUN_EVENT_PAGE_SIZE = 1_000;

type RunEventLoadResult = {
  events: SystemAgentStreamEvent[];
  foldedCount: number;
  lastScannedSeq: number;
};

async function loadRunEvents(
  runId: string,
  lastSeq?: number,
  previous?: RunEventLoadResult,
): Promise<RunEventLoadResult> {
  const bySeq = new Map<number, SystemAgentStreamEvent>(
    (previous?.events || []).map((event) => [Number(event.seq || 0), event]),
  );
  let cursor = previous?.lastScannedSeq || 0;
  let foldedCount = previous?.foldedCount || 0;
  const knownLastSeq = typeof lastSeq === "number" && lastSeq > 0 ? lastSeq : null;

  while (knownLastSeq == null || cursor < knownLastSeq) {
    const pageStartCursor = cursor;
    const page = await listSystemAgentRunEvents(runId, cursor, RUN_EVENT_PAGE_SIZE);
    for (const event of page) {
      if (isPerspectiveNoiseEvent(event)) foldedCount += 1;
      else bySeq.set(Number(event.seq || 0), event);
    }
    cursor = Number(page.at(-1)?.seq || pageStartCursor);
    if (page.length < RUN_EVENT_PAGE_SIZE || cursor <= pageStartCursor) break;
  }

  const events = [...bySeq.values()].sort((left, right) => Number(left.seq || 0) - Number(right.seq || 0));
  return {
    events,
    foldedCount,
    lastScannedSeq: cursor,
  };
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
  runStatus,
  lastSeq,
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
  runStatus?: string;
  lastSeq?: number;
  className?: string;
  defaultOpen?: boolean;
  mode?: "compact" | "diagnostic";
  timezone?: string;
}) {
  const [open, setOpen] = useState(Boolean(defaultOpen ?? (running || failed)));
  const queryClient = useQueryClient();
  const requestedTargetRef = useRef<{ runId: string; lastSeq: number } | null>(null);

  useEffect(() => {
    if (running) setOpen(true);
    else if (failed) setOpen(true);
    else if (!running && !failed && liveEvents?.length) setOpen(false);
  }, [running, failed, liveEvents?.length]);

  const queryKey = ["system-agent", "run-events", runId] as const;
  const q = useQuery({
    queryKey,
    queryFn: () => loadRunEvents(
      runId!,
      lastSeq,
      queryClient.getQueryData<RunEventLoadResult>(queryKey),
    ),
    enabled: Boolean(runId) && open && !liveEvents?.length,
    staleTime: 30_000,
  });

  useEffect(() => {
    if (!runId || !open || liveEvents?.length || q.isFetching || typeof lastSeq !== "number") return;
    if (lastSeq <= (q.data?.lastScannedSeq || 0)) return;
    const requested = requestedTargetRef.current;
    if (requested?.runId === runId && requested.lastSeq >= lastSeq) return;
    requestedTargetRef.current = { runId, lastSeq };
    void q.refetch();
  }, [lastSeq, liveEvents?.length, open, q.data?.lastScannedSeq, q.isFetching, q.refetch, runId]);

  const events: TraceEvent[] = useMemo(() => {
    if (liveEvents?.length) return liveEvents as TraceEvent[];
    return (q.data?.events || []) as TraceEvent[];
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
        ) : mode === "diagnostic" ? (
          <AgentRunPerspective
            events={events}
            running={running}
            failed={failed || summary.failed}
            runStatus={runStatus}
            traceNotice={q.data?.foldedCount
              ? `已折叠 ${new Intl.NumberFormat("zh-CN").format(q.data.foldedCount)} 条心跳与流式事件。`
              : undefined}
            timezone={timezone}
          />
        ) : (
          <ol className={cn(
            "space-y-1 overflow-y-auto rounded-md border bg-muted/20 px-2 py-1.5",
            "max-h-48",
          )}>
            {visible.map((event, index) => (
              <li key={`${event.seq ?? index}-${event.type}`} className="text-[11px] leading-4">
                <div className="flex min-w-0 flex-wrap items-baseline gap-x-1.5">
                  <span className="tabular-nums text-muted-foreground/80">
                    {typeof event.seq === "number" ? `#${event.seq}` : ""}
                  </span>
                  <span>{labelEvent(event)}</span>
                </div>
              </li>
            ))}
          </ol>
        )
      ) : null}
    </div>
  );
}
