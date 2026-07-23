import { useQuery } from "@tanstack/react-query";

import { listSystemAgentRunEvents, type SystemAgentStreamEvent } from "@/api/systemAgent";
import { Skeleton } from "@/components/ui/misc";
import { systemAgentToolLabel } from "@/lib/systemAgentLabels";

function labelEvent(event: SystemAgentStreamEvent): string {
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

export function RunTrace({ runId }: { runId: string }) {
  const q = useQuery({
    queryKey: ["system-agent", "run-events", runId],
    queryFn: () => listSystemAgentRunEvents(runId),
    staleTime: 30_000,
  });

  if (q.isLoading) {
    return (
      <div className="rounded-md border bg-muted/20 p-2">
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="mt-2 h-4 w-1/2" />
      </div>
    );
  }
  if (q.isError) {
    return (
      <div className="rounded-md border border-destructive/30 bg-destructive/5 px-2 py-1.5 text-[11px] text-destructive">
        无法加载执行轨迹
      </div>
    );
  }
  const events = (q.data || []).filter((e) =>
    [
      "route_selected",
      "skill_selected",
      "tool_started",
      "tool_finished",
      "provider_selected",
      "model_attempt",
      "retry_scheduled",
      "model_exhausted",
      "assistant_delta_reset",
      "assistant_message",
      "error",
      "done",
    ].includes(String(e.type)),
  );

  if (!events.length) {
    return (
      <div className="rounded-md border bg-muted/20 px-2 py-1.5 text-[11px] text-muted-foreground">
        暂无轨迹事件
      </div>
    );
  }

  return (
    <ol className="max-h-48 space-y-1 overflow-y-auto rounded-md border bg-muted/20 px-2 py-1.5">
      {events.map((event, index) => (
        <li key={`${event.seq ?? index}-${event.type}`} className="text-[11px] leading-4">
          <span className="tabular-nums text-muted-foreground/80">
            {typeof event.seq === "number" ? `#${event.seq} ` : ""}
          </span>
          {labelEvent(event)}
        </li>
      ))}
    </ol>
  );
}
