// 全局横幅：显示前后端构建版本不一致与 KillSwitch 全局总闸。
//
// 设计：
//  - 单独组件，不耦合 TopBar；放在 AppShell 内顶端
//  - KillSwitch 与 TopBar 按钮共享 react-query cache key，点切换会立即联动
//  - 都不显示时返回 null（不占空间）
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, ShieldAlert } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import { getBackendVersion } from "@/api/system";
import { api, getErrMsg } from "@/lib/api";
import { APP_VERSION } from "@/lib/version";

interface KillSwitchState {
  enabled: boolean;
}

async function fetchKillSwitch(): Promise<KillSwitchState> {
  const { data } = await api.get<KillSwitchState>("/api/system/kill-switch");
  return data;
}

export function GlobalAlertBar() {
  return (
    <>
      <VersionMismatchBar />
      <KillSwitchBar />
    </>
  );
}

function VersionMismatchBar() {
  const { data, error } = useQuery({
    queryKey: ["system", "version"],
    queryFn: getBackendVersion,
    refetchInterval: 60_000,
    refetchIntervalInBackground: false,
    retry: 1,
    refetchOnWindowFocus: true,
  });

  if (error || !data || data.version === APP_VERSION) return null;
  return <VersionMismatchContent backendVersion={data.version} />;
}

async function hardRefreshWithoutSw(): Promise<void> {
  if ("serviceWorker" in navigator) {
    try {
      const registrations = await navigator.serviceWorker.getRegistrations();
      await Promise.all(registrations.map((registration) => registration.unregister()));
    } catch {
      // 继续清理其它缓存并强制刷新。
    }
  }
  if ("caches" in window) {
    try {
      const keys = await caches.keys();
      await Promise.all(keys.map((key) => caches.delete(key)));
    } catch {
      // Cache Storage 不可用时仍继续强制刷新。
    }
  }
  const target = new URL(window.location.href);
  target.searchParams.set("_v", String(Date.now()));
  window.location.replace(target.toString());
}

function VersionMismatchContent({ backendVersion }: { backendVersion: string }) {
  return (
    <div
      role="alert"
      className="flex items-center justify-between gap-3 border-b border-warning/40 bg-warning/10 px-4 py-2 text-sm text-warning"
    >
      <div className="flex min-w-0 items-center gap-2">
        <RefreshCw className="h-4 w-4 shrink-0" />
        <span className="font-medium">前端资源需要刷新</span>
        <span className="hidden text-warning sm:inline">
          前端 v{APP_VERSION} · 后端 v{backendVersion}，请清理旧 PWA 缓存
        </span>
      </div>
      <Button
        size="sm"
        variant="outline"
        className="shrink-0 border-warning/50 bg-warning/15 hover:bg-warning/25"
        onClick={() => void hardRefreshWithoutSw()}
      >
        清缓存重载
      </Button>
    </div>
  );
}

// ── KillSwitch 总闸 ────────────────────────────────────────────
function KillSwitchBar() {
  const qc = useQueryClient();
  const { data } = useQuery({
    queryKey: ["system", "kill-switch"],
    queryFn: fetchKillSwitch,
    refetchInterval: 30_000,
    refetchIntervalInBackground: false,
  });

  const mut = useMutation({
    mutationFn: async () => {
      await api.post("/api/system/kill-switch", { enabled: false });
    },
    onSuccess: () => {
      toast.success("已恢复运行");
      qc.invalidateQueries({ queryKey: ["system", "kill-switch"] });
      qc.invalidateQueries({ queryKey: ["accounts"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  if (!data?.enabled) return null;

  return (
    <div
      role="alert"
      className="
        flex items-center justify-between gap-3
        border-b border-destructive/40 bg-destructive/10 px-4 py-2
        text-sm text-destructive
      "
    >
      <div className="flex min-w-0 items-center gap-2">
        <ShieldAlert className="h-4 w-4 shrink-0" />
        <span className="font-medium">全局总闸已开启</span>
        <span className="hidden text-muted-foreground sm:inline">
          所有账号 worker 已停止，解除后自动恢复 active 账号
        </span>
      </div>
      <Button
        size="sm"
        variant="outline"
        className="shrink-0"
        disabled={mut.isPending}
        onClick={() => {
          if (confirm("确认恢复全部账号运行？")) mut.mutate();
        }}
      >
        恢复运行
      </Button>
    </div>
  );
}
