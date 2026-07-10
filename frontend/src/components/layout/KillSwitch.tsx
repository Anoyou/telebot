// 顶部紧急停用按钮：调 POST /api/system/kill-switch 切换全局总闸
import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ShieldAlert, ShieldCheck } from "lucide-react";
import { toast } from "sonner";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { api, getErrMsg } from "@/lib/api";
import { cn } from "@/lib/utils";

interface KillSwitchState {
  enabled: boolean;
}

async function fetchKillSwitch(): Promise<KillSwitchState> {
  const { data } = await api.get<KillSwitchState>("/api/system/kill-switch");
  return data;
}

export function KillSwitch({ compact = false }: { compact?: boolean }) {
  const qc = useQueryClient();
  const [confirmOpen, setConfirmOpen] = useState(false);
  // 实时显示总闸状态；轻量轮询：30s 刷新
  const { data } = useQuery({
    queryKey: ["system", "kill-switch"],
    queryFn: fetchKillSwitch,
    refetchInterval: 30_000,
    refetchIntervalInBackground: false,
  });
  const enabled = !!data?.enabled;

  const mut = useMutation({
    mutationFn: async (next: boolean) => {
      await api.post("/api/system/kill-switch", { enabled: next });
    },
    onSuccess: (_, next) => {
      toast.success(next ? "已开启紧急停用：所有账号 worker 已停止" : "已恢复运行");
      setConfirmOpen(false);
      qc.invalidateQueries({ queryKey: ["system", "kill-switch"] });
      qc.invalidateQueries({ queryKey: ["accounts"] });
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });

  const requestToggle = () => {
    if (mut.isPending) return;
    if (enabled) {
      mut.mutate(false);
      return;
    }
    setConfirmOpen(true);
  };

  return (
    <>
      <Button
        variant="outline"
        size="sm"
        className={cn(
          "h-10 rounded-full bg-card text-xs font-semibold shadow-sm hover:bg-card hover:shadow-md",
          compact ? "w-10 px-0" : "w-10 px-0 sm:w-auto sm:gap-2 sm:px-3",
          enabled
            ? "border-destructive/30 bg-destructive/10 text-destructive hover:bg-destructive/[0.18]"
            : "border-destructive/30 text-destructive hover:border-destructive/50 hover:bg-destructive/10",
        )}
        title={enabled ? "恢复全部账号 worker" : "紧急停用全部账号 worker"}
        aria-label={enabled ? "恢复全部账号 worker" : "紧急停用全部账号 worker"}
        onClick={requestToggle}
      >
        {enabled ? (
          <>
            <ShieldCheck className="h-4 w-4" />
            {compact ? null : <span className="hidden sm:inline">恢复运行</span>}
          </>
        ) : (
          <>
            <ShieldAlert className="h-4 w-4" />
            {compact ? null : <span className="hidden sm:inline">紧急停用</span>}
          </>
        )}
      </Button>
      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent className="max-w-md">
          <DialogHeader>
            <DialogTitle className="text-destructive">
              确认紧急停用？
            </DialogTitle>
            <DialogDescription>
              所有账号 worker 会立即停止，Telegram 侧的自动回复、转发、定时任务和 AI
              指令都会暂停。
            </DialogDescription>
          </DialogHeader>
          <div className="rounded-xl border border-destructive/30 bg-destructive/10 px-3 py-2 text-sm text-destructive">
            这是全局总闸。确认后可从同一个按钮恢复运行。
          </div>
          <DialogFooter className="gap-2">
            <Button
              variant="outline"
              onClick={() => setConfirmOpen(false)}
              disabled={mut.isPending}
            >
              取消
            </Button>
            <Button
              variant="destructive"
              onClick={() => mut.mutate(true)}
              disabled={mut.isPending}
            >
              确认停用
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
