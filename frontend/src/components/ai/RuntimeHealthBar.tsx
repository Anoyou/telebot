import { useQuery } from "@tanstack/react-query";
import { HeartPulse } from "lucide-react";

import { listProviderRuntimeHealth } from "@/api/commands";
import { MetaBadge } from "@/components/ui/meta-badge";
import { Skeleton } from "@/components/ui/misc";
import { cn } from "@/lib/utils";

/**
 * 测活页「运行时健康」只读栏：展示 Agent 路径写入的 healthy/cooling。
 * 测活本身不改写这些状态。
 */
export function RuntimeHealthBar({ className }: { className?: string }) {
  const q = useQuery({
    queryKey: ["llm-providers", "runtime-health"],
    queryFn: listProviderRuntimeHealth,
    staleTime: 15_000,
    refetchInterval: 30_000,
  });

  const rows = q.data || [];
  const cooling = rows.filter((r) => r.state === "cooling");
  const credential = rows.filter((r) => r.last_error_class === "credential");

  return (
    <section
      className={cn(
        "rounded-lg border bg-card p-3 shadow-sm",
        className,
      )}
      aria-label="运行时健康"
    >
      <div className="flex flex-wrap items-center gap-2">
        <HeartPulse className="h-4 w-4 text-muted-foreground" />
        <span className="text-sm font-semibold">运行时健康</span>
        <span className="text-[11px] text-muted-foreground">
          仅 Agent 真实调用；测活不写入
        </span>
        {cooling.length > 0 ? (
          <MetaBadge tone="warn">冷却 {cooling.length}</MetaBadge>
        ) : (
          <MetaBadge tone="success">无冷却</MetaBadge>
        )}
        {credential.length > 0 ? (
          <MetaBadge tone="danger">凭据异常 {credential.length}</MetaBadge>
        ) : null}
      </div>
      {q.isLoading ? (
        <div className="mt-2 space-y-1">
          <Skeleton className="h-4 w-2/3" />
          <Skeleton className="h-4 w-1/2" />
        </div>
      ) : rows.length === 0 ? (
        <p className="mt-2 text-xs text-muted-foreground">
          暂无运行时记录（尚未有业务调用失败或成功采样）。
        </p>
      ) : (
        <ul className="mt-2 max-h-36 space-y-1 overflow-y-auto text-[11px] leading-4">
          {rows
            .slice()
            .sort((a, b) => {
              const ac = a.state === "cooling" ? 0 : 1;
              const bc = b.state === "cooling" ? 0 : 1;
              return ac - bc || a.provider_id - b.provider_id;
            })
            .map((row) => (
              <li
                key={`${row.provider_id}::${row.model}`}
                className="flex flex-wrap items-center gap-x-2 gap-y-0.5 border-b border-border/40 py-1 last:border-0"
              >
                <span className="font-mono tabular-nums text-muted-foreground">
                  #{row.provider_id}
                </span>
                <span className="break-all font-mono">{row.model || "—"}</span>
                <MetaBadge
                  tone={row.state === "cooling" ? "warn" : "success"}
                  mono
                >
                  {row.state === "cooling"
                    ? `冷却 ${row.cooldown_remaining_seconds ?? "?"}s`
                    : "healthy"}
                </MetaBadge>
                {row.last_error_class ? (
                  <span className="text-muted-foreground">
                    {row.last_error_class}
                    {row.last_error_message
                      ? ` · ${String(row.last_error_message).slice(0, 80)}`
                      : ""}
                  </span>
                ) : null}
              </li>
            ))}
        </ul>
      )}
    </section>
  );
}
