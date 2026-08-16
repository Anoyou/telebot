import { useState } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { ChevronDown, ShieldAlert } from "lucide-react";
import { toast } from "sonner";

import {
  applyRuntimeProfile,
  dryRunRuntimeProfile,
  getRuntimeProfile,
  restoreRuntimeProfile,
} from "@/api/system";
import type { RuntimeProfileDryRunOut } from "@/api/types";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from "@/components/ui/dialog";
import { SectionHeader, SignalPill } from "@/components/ui/status";
import { getErrMsg } from "@/lib/api";
import { moduleLabel } from "@/lib/navigation";

const STATUS_LABEL = {
  idle: "空闲",
  applying: "正在进入",
  active: "值守中",
  restoring: "正在恢复",
  failed: "收敛失败",
} as const;

export function AccountRuntimePanel() {
  const qc = useQueryClient();
  const [expanded, setExpanded] = useState(false);
  const [confirmOpen, setConfirmOpen] = useState(false);
  const [preview, setPreview] = useState<RuntimeProfileDryRunOut | null>(null);
  const profileQ = useQuery({
    queryKey: ["system", "runtime-profile"],
    queryFn: getRuntimeProfile,
    staleTime: 5_000,
  });

  const invalidate = () => {
    qc.invalidateQueries({ queryKey: ["system", "runtime-profile"] });
    qc.invalidateQueries({ queryKey: ["system", "capabilities"] });
    qc.invalidateQueries({ queryKey: ["system", "settings"] });
    qc.invalidateQueries({ queryKey: ["platform", "tree"] });
    qc.invalidateQueries({ queryKey: ["matrix"] });
    qc.invalidateQueries({ queryKey: ["account"] });
  };

  const previewMut = useMutation({
    mutationFn: () => dryRunRuntimeProfile("safe_watch"),
    onSuccess: (data) => {
      setPreview(data);
      setConfirmOpen(true);
    },
    onError: (err) => toast.error(getErrMsg(err)),
  });
  const applyMut = useMutation({
    mutationFn: () => applyRuntimeProfile("safe_watch"),
    onSuccess: () => {
      setConfirmOpen(false);
      toast.success("值守模式已激活：插件叶投递、定时任务和资金动作已冻结");
      invalidate();
    },
    onError: (err) => {
      toast.error(getErrMsg(err));
      invalidate();
    },
  });
  const restoreMut = useMutation({
    mutationFn: restoreRuntimeProfile,
    onSuccess: () => {
      toast.success("已恢复值守前快照，插件投递与定时任务恢复");
      invalidate();
    },
    onError: (err) => {
      toast.error(getErrMsg(err));
      invalidate();
    },
  });

  const profile = profileQ.data;
  const safeWatch = profile?.active_profile === "safe_watch";
  const statusLabel = profile ? STATUS_LABEL[profile.status] : "读取中";
  const profileLabel = safeWatch ? "值守" : profile?.current_profile === "custom" ? "自定义" : "生产";

  return (
    <>
      <div className={`rounded-lg border bg-card/60 ${safeWatch ? "border-warning/50" : ""}`}>
        <div className="p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <button
              type="button"
              className="min-w-0 flex-1 text-left"
              onClick={() => setExpanded((value) => !value)}
              aria-expanded={expanded}
            >
              <SectionHeader
                icon={ShieldAlert}
                title="运行模式"
                description="一键进入可持久、自愈的值守态，恢复时逐项还原进入前快照。"
                descriptionClassName="max-sm:hidden"
              />
            </button>
            <div className="flex shrink-0 items-center gap-2">
              <SignalPill
                tone={profile?.status === "failed" ? "danger" : safeWatch ? "warn" : "success"}
                label="当前"
                value={profileQ.isLoading ? "读取中" : profileLabel}
              />
              <Button
                type="button"
                variant="ghost"
                size="icon"
                className="h-8 w-8"
                onClick={() => setExpanded((value) => !value)}
                aria-label={expanded ? "收起运行模式" : "展开运行模式"}
              >
                <ChevronDown className={`h-4 w-4 transition-transform ${expanded ? "rotate-180" : ""}`} />
              </Button>
            </div>
          </div>
        </div>
        {expanded ? (
          <div className="space-y-4 border-t px-4 py-4">
            <div className="grid gap-2 text-xs sm:grid-cols-3">
              <div className="rounded-md border bg-muted/20 p-3">
                <div className="text-muted-foreground">投递与任务</div>
                <div className="mt-1 font-medium">{safeWatch ? "已暂停" : "按配置运行"}</div>
              </div>
              <div className="rounded-md border bg-muted/20 p-3">
                <div className="text-muted-foreground">资金动作</div>
                <div className="mt-1 font-medium">{safeWatch ? "已拒绝" : "按能力闸运行"}</div>
              </div>
              <div className="rounded-md border bg-muted/20 p-3">
                <div className="text-muted-foreground">状态</div>
                <div className="mt-1 font-medium">{statusLabel}</div>
              </div>
            </div>
            {safeWatch ? (
              <div className="rounded-md border border-warning/30 bg-warning/10 p-3 text-xs leading-5">
                {profile?.blind_spot}
              </div>
            ) : null}
            {profile?.last_error ? (
              <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-xs text-destructive">
                收敛失败：{profile.last_error}
              </div>
            ) : null}
            <div className="flex flex-wrap items-center justify-between gap-3">
              <p className="max-w-2xl text-xs leading-5 text-muted-foreground">
                值守中 userbot 直通与命令入站继续纯观测落库，插件叶零投递。平台通知与内置管理命令保留，可随时查询并退出值守。
              </p>
              {safeWatch ? (
                <Button
                  variant="outline"
                  onClick={() => restoreMut.mutate()}
                  loading={restoreMut.isPending}
                  loadingText="正在恢复"
                  disabled={profile?.status === "restoring" || profile?.status === "applying"}
                >
                  恢复值守前快照
                </Button>
              ) : (
                <Button
                  onClick={() => previewMut.mutate()}
                  loading={previewMut.isPending}
                  loadingText="正在预检"
                  disabled={profileQ.isLoading || profile?.status === "applying"}
                >
                  进入值守模式
                </Button>
              )}
            </div>
          </div>
        ) : null}
      </div>

      <Dialog open={confirmOpen} onOpenChange={setConfirmOpen}>
        <DialogContent className="max-w-lg">
          <DialogHeader>
            <DialogTitle>确认进入值守模式</DialogTitle>
            <DialogDescription>
              值守会暂停插件叶投递与定时任务，并注册资金动作拒绝原因。平台观测、告警通知和内置管理命令仍可用。
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-3 text-sm">
            <div className="rounded-md border bg-muted/20 p-3">
              <div className="font-medium">模块变更预览</div>
              {preview?.diff.length ? (
                <div className="mt-2 space-y-1.5">
                  {preview.diff.map((item) => (
                    <div key={item.key} className="flex items-center justify-between gap-3 text-xs">
                      <span>{moduleLabel(item.key)}</span>
                      <span className="text-muted-foreground">{item.from_enabled ? "开启" : "关闭"} → {item.to_enabled ? "开启" : "关闭"}</span>
                    </div>
                  ))}
                </div>
              ) : (
                <p className="mt-2 text-xs text-muted-foreground">模块开关无需调整，仍会执行 worker 暂停收敛与资金冻结。</p>
              )}
            </div>
            <div className="rounded-md border border-warning/30 bg-warning/10 p-3 text-xs leading-5">{preview?.blind_spot}</div>
          </div>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmOpen(false)} disabled={applyMut.isPending}>取消</Button>
            <Button onClick={() => applyMut.mutate()} loading={applyMut.isPending} loadingText="正在收敛">确认进入值守</Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  );
}
