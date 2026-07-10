// 网络环境徽章：显示当前后端进程出口 IP 的国家/地区，hover 看详情
import { useQuery, useQueryClient, useMutation } from "@tanstack/react-query";
import { Globe2, Loader2, RefreshCw, AlertTriangle } from "lucide-react";

import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { getNetworkInfo, refreshNetworkInfo } from "@/api/network";
import { cn } from "@/lib/utils";

// 把国家代码 → emoji 国旗（仅 ISO-2 码）。失败回退 🌐
function flagOf(country?: string | null): string {
  if (!country || country.length !== 2) return "🌐";
  const cp = (s: string) => 0x1f1e6 + (s.toUpperCase().charCodeAt(0) - 65);
  try {
    return String.fromCodePoint(cp(country[0]), cp(country[1]));
  } catch {
    return "🌐";
  }
}

export function NetworkBadge() {
  const qc = useQueryClient();
  const q = useQuery({
    queryKey: ["system", "network"],
    queryFn: getNetworkInfo,
    staleTime: 60_000,
    refetchInterval: 5 * 60_000,
    refetchIntervalInBackground: false,
  });
  const refreshMut = useMutation({
    mutationFn: refreshNetworkInfo,
    onSuccess: (d) => qc.setQueryData(["system", "network"], d),
  });

  const data = q.data;
  const flag = flagOf(data?.country);
  const hasError = !!data?.error || (!q.isLoading && !data?.ip);

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="sm"
          className="h-8 gap-1 px-2 text-xs"
          title="主进程出口（账号自带代理时不算这里）"
        >
          {q.isLoading ? (
            <Loader2 className="h-3.5 w-3.5 animate-spin" />
          ) : hasError ? (
            <AlertTriangle className="h-3.5 w-3.5 text-warning" />
          ) : (
            <span className="text-base leading-none">{flag}</span>
          )}
          <span
            className={cn(
              "font-mono",
              hasError && "text-warning",
            )}
          >
            {q.isLoading
              ? "探测中"
              : hasError
                ? "未知"
                : data?.country || "?"}
          </span>
          {!hasError && data?.ip ? (
            <span className="text-muted-foreground hidden sm:inline">
              · 主进程
            </span>
          ) : null}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-[min(280px,calc(100vw-2rem))] p-3">
        <div className="space-y-2 text-xs">
          <div className="flex items-center gap-2 border-b pb-2">
            <Globe2 className="h-4 w-4 text-muted-foreground" />
            <span className="text-sm font-medium">主进程出网环境</span>
          </div>
          <div className="rounded-md border px-2 py-1.5 text-[11px] alert-warning">
            ⚠ 这里只代表 web 后端进程的直连出口；每个 TG 账号若绑定了代理，
            出口走的是该代理（详见每张账号卡上的"代理"行）。
          </div>
          {hasError ? (
            <div className="space-y-1">
              <div className="font-medium">⚠ 探测失败</div>
              <div className="text-muted-foreground break-all">
                {data?.error || "未拿到出口 IP（可能后端无外网）"}
              </div>
            </div>
          ) : (
            <dl className="grid grid-cols-[64px_1fr] gap-y-1.5">
              <dt className="text-muted-foreground">IP</dt>
              <dd className="font-mono">{data?.ip || "-"}</dd>
              <dt className="text-muted-foreground">国家</dt>
              <dd>
                {flag} {data?.country || "-"}
              </dd>
              <dt className="text-muted-foreground">地区</dt>
              <dd>{data?.region || "-"}</dd>
              <dt className="text-muted-foreground">城市</dt>
              <dd>{data?.city || "-"}</dd>
              <dt className="text-muted-foreground">ISP</dt>
              <dd className="break-all">{data?.org || "-"}</dd>
              <dt className="text-muted-foreground">缓存</dt>
              <dd className="text-muted-foreground">
                {data?.fresh ? "本次新拉" : "5min 缓存"}
              </dd>
            </dl>
          )}
          <div className="flex items-center justify-end border-t pt-2">
            <Button
              variant="ghost"
              size="sm"
              className="h-6 gap-1 text-xs"
              disabled={refreshMut.isPending}
              onClick={() => refreshMut.mutate()}
            >
              <RefreshCw
                className={cn(
                  "h-3 w-3",
                  refreshMut.isPending && "animate-spin",
                )}
              />
              刷新
            </Button>
          </div>
        </div>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
