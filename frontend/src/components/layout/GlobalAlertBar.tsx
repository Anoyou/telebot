// 全局横幅：依次叠加显示
//   1. 版本不一致（红条，最优先 — 这种情况下其他功能可能跑老 schema 不准）
//   2. KillSwitch 全局总闸（红条）
//
// 设计：
//  - 单独组件，不耦合 TopBar；放在 AppShell 内顶端
//  - KillSwitch 与 TopBar 按钮共享 react-query cache key，点切换会立即联动
//  - 版本检测每 60s 拉一次，启动时即拉
//  - 都不显示时返回 null（不占空间）
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { RefreshCw, ShieldAlert } from "lucide-react";
import { useEffect, useRef } from "react";
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

// ── 版本不一致检测 ──────────────────────────────────────────────
// 后端 GET /api/system/version 是 public（无鉴权），前端启动就能调
function VersionMismatchBar() {
  const { data, error } = useQuery({
    queryKey: ["system", "version"],
    queryFn: getBackendVersion,
    refetchInterval: 60_000, // 1 分钟轮询；不一致时由用户手动操作即可
    refetchIntervalInBackground: false,
    retry: 1,
    // 后端短暂不可达时不弹红条（重启间隙），仅在拿到响应且不一致时弹
    refetchOnWindowFocus: true,
  });

  // 网络错 / 后端未起 → 不弹（避免开发期持续闪屏）
  if (error || !data) return null;

  // 一致就闭嘴
  if (data.version === APP_VERSION) return null;

  return <VersionMismatchContent backendVersion={data.version} />;
}

async function hardRefreshWithoutSw(): Promise<void> {
  // 先注销 SW，避免继续命中旧缓存
  if ("serviceWorker" in navigator) {
    try {
      const registrations = await navigator.serviceWorker.getRegistrations();
      await Promise.all(registrations.map((reg) => reg.unregister()));
    } catch {
      // 忽略并继续强刷
    }
  }

  // 再清理 Cache Storage
  if ("caches" in window) {
    try {
      const keys = await caches.keys();
      await Promise.all(keys.map((key) => caches.delete(key)));
    } catch {
      // 忽略并继续强刷
    }
  }

  // 强制整页重载
  window.location.replace(`${window.location.pathname}?_v=${Date.now()}${window.location.hash}`);
}

function VersionMismatchContent({ backendVersion }: { backendVersion: string }) {
  const autoAttemptedRef = useRef(false);

  useEffect(() => {
    if (autoAttemptedRef.current) return;
    autoAttemptedRef.current = true;

    // 每个版本差异组合仅自动修复一次，避免极端情况下循环刷新。
    const guardKey = `telepilot-version-sync-attempt:${APP_VERSION}->${backendVersion}`;
    if (sessionStorage.getItem(guardKey) === "1") return;
    sessionStorage.setItem(guardKey, "1");

    void hardRefreshWithoutSw();
  }, [backendVersion]);

  return (
    <div
      role="alert"
      className="
        flex items-center justify-between gap-3
        border-b border-warning/40 bg-warning/10 px-4 py-2 text-sm text-warning
      "
    >
      <div className="flex min-w-0 items-center gap-2">
        <RefreshCw className="h-4 w-4 shrink-0" />
        <span className="font-medium">前后端版本不一致</span>
        <span className="hidden text-warning sm:inline">
          前端 v{APP_VERSION} · 后端 v{backendVersion}
          {" — 正在自动清缓存并刷新，如未恢复可手动修复"}
        </span>
      </div>
      <Button
        size="sm"
        variant="outline"
        className="shrink-0 border-warning/50 bg-warning/15 hover:bg-warning/25"
        onClick={() => void hardRefreshWithoutSw()}
      >
        手动修复
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
