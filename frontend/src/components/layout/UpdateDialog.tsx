import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, RefreshCw, RotateCcw, CheckCircle2, AlertCircle } from "lucide-react";

import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { checkUpdate, getUpdateJob, pullUpdate, restartApp } from "@/api/system";
import type {
  CheckUpdateResult,
  PullUpdateResult,
  UpdateJobStatus,
} from "@/api/types";
import { APP_VERSION, APP_VERSION_LABEL } from "@/lib/version";
import { checkFrontendUpdate } from "@/pwa";

type UpdateActionRequired =
  | "none"
  | "docs_only"
  | "frontend"
  | "backend"
  | "mixed"
  | "full_update"
  | "manual"
  | "unsupported"
  | "restart";

interface UpdatePlanMeta {
  runtimeMode: string | null;
  actionRequired: UpdateActionRequired;
  planLabel: string | null;
  planDetail: string | null;
  components: string[];
  requiresFullUpdate: boolean;
  requiresBackup: boolean;
  canApply: boolean;
  manualCommand: string | null;
  remote: string | null;
  branch: string | null;
  updateExecutor: string | null;
}

type Step =
  | { kind: "checking" }
  | { kind: "up_to_date"; commit: string }
  | { kind: "cannot_check"; plan: UpdatePlanMeta }
  | { kind: "has_update"; current: string; remote: string; ahead: number; changedFiles: string[]; plan: UpdatePlanMeta }
  | { kind: "pulling" }
  | { kind: "job_running"; jobId: string; status: string; logs: string[]; plan: UpdatePlanMeta }
  | { kind: "pulled"; newCommit: string | null; summary: string | null; plan: UpdatePlanMeta }
  | { kind: "pull_failed"; error: string }
  | { kind: "check_failed"; error: string }
  | { kind: "restarting"; countdown: number };

type FrontendUpdateState =
  | "idle"
  | "checking"
  | "updating"
  | "up_to_date"
  | "unsupported"
  | "error";

interface UpdateDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

export function UpdateDialog({ open, onOpenChange }: UpdateDialogProps) {
  const [step, setStep] = useState<Step | null>(null);
  const [frontendUpdateState, setFrontendUpdateState] = useState<FrontendUpdateState>("idle");
  const timerRef = useRef<ReturnType<typeof setInterval>>();
  const jobPollTokenRef = useRef(0);

  const normalizeAction = (raw: CheckUpdateResult["action_required"]): UpdateActionRequired => {
    return typeof raw === "string" ? raw : "none";
  };

  const parsePlanMeta = (res: CheckUpdateResult | PullUpdateResult): UpdatePlanMeta => ({
    runtimeMode: res.runtime_mode ?? null,
    actionRequired: normalizeAction(res.action_required),
    planLabel: res.plan_label ?? null,
    planDetail: res.plan_detail ?? null,
    components: res.components ?? [],
    requiresFullUpdate: Boolean(res.requires_full_update),
    requiresBackup: Boolean(res.requires_backup),
    canApply: res.can_apply ?? true,
    manualCommand: res.manual_command ?? null,
    remote: res.remote ?? null,
    branch: res.branch ?? null,
    updateExecutor: res.update_executor ?? null,
  });

  const getPrimaryActionLabel = (plan: UpdatePlanMeta) => {
    if (plan.manualCommand) {
      return "复制服务器命令";
    }
    if (plan.actionRequired === "manual" || plan.actionRequired === "unsupported") {
      return "查看服务器命令";
    }
    if (!plan.canApply) {
      return "查看更新说明";
    }
    switch (plan.actionRequired) {
      case "restart":
        return "重启使更新生效";
      case "backend":
        if (plan.runtimeMode === "local_source") return "拉取并重启使更新生效";
        return "增量重建并重启后端";
      case "frontend":
        if (plan.runtimeMode === "local_source") return "拉取并重启使更新生效";
        return "增量重建前端";
      case "full_update":
        if (plan.runtimeMode === "local_source") return "拉取并重启使更新生效";
        return "执行完整更新";
      case "mixed":
        if (plan.runtimeMode === "local_source") return "拉取并重启使更新生效";
        return "执行增量更新";
      case "docs_only":
        return "应用文档更新";
      case "none":
      default:
        return "应用更新";
    }
  };

  const isManualRuntime = (plan: UpdatePlanMeta) =>
    plan.actionRequired === "manual" ||
    plan.actionRequired === "unsupported" ||
    plan.runtimeMode === "prod_container_manual";

  const describeUpdateState = (plan: UpdatePlanMeta, ahead: number) => {
    if (isManualRuntime(plan) && ahead <= 0) {
      return "需要在服务器执行更新";
    }
    return "发现新版本可用";
  };

  const getFrontendUpdateMessage = () => {
    switch (frontendUpdateState) {
      case "up_to_date":
        return "已是最新版本";
      case "updating":
        return "发现新版本，页面即将自动刷新";
      case "unsupported":
        return "当前环境不支持（开发模式或浏览器不支持）";
      case "error":
        return "检查失败，请稍后重试";
      default:
        return null;
    }
  };

  const getFrontendUpdateMessageClassName = () => {
    switch (frontendUpdateState) {
      case "up_to_date":
        return "text-success";
      case "updating":
      case "unsupported":
        return "text-warning";
      case "error":
        return "text-destructive";
      default:
        return "text-muted-foreground";
    }
  };

  const doCheckFrontendUpdate = async () => {
    setFrontendUpdateState("checking");
    try {
      const result = await checkFrontendUpdate();
      setFrontendUpdateState(result);
    } catch {
      setFrontendUpdateState("error");
    }
  };

  // 打开时自动检查更新
  const doCheck = useCallback(async () => {
    setStep({ kind: "checking" });
    try {
      const res: CheckUpdateResult = await checkUpdate();
      if (res.error) {
        setStep({ kind: "check_failed", error: res.error });
      } else if (res.can_check === false) {
        setStep({ kind: "cannot_check", plan: parsePlanMeta(res) });
      } else if (!res.has_update) {
        setStep({ kind: "up_to_date", commit: res.current_commit || "?" });
      } else {
        setStep({
          kind: "has_update",
          current: res.current_commit || "?",
          remote: res.remote_commit || "?",
          ahead: res.ahead,
          changedFiles: res.changed_files ?? [],
          plan: parsePlanMeta(res),
        });
      }
    } catch (e) {
      setStep({
        kind: "check_failed",
        error: e instanceof Error ? e.message : String(e),
      });
    }
  }, []);

  useEffect(() => {
    if (open) {
      setFrontendUpdateState("idle");
      doCheck();
    } else {
      setStep(null);
      setFrontendUpdateState("idle");
      jobPollTokenRef.current += 1;
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = undefined;
      }
    }
    return () => {
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = undefined;
      }
    };
  }, [open, doCheck]);

  const doPull = async () => {
    setStep({ kind: "pulling" });
    try {
      const res: PullUpdateResult = await pullUpdate();
      if (res.success) {
        const plan = parsePlanMeta(res);
        if (res.job_id) {
          setStep({
            kind: "job_running",
            jobId: res.job_id,
            status: res.status || "queued",
            logs: [],
            plan,
          });
          pollUpdateJob(res.job_id, plan);
          return;
        }
        setStep({ kind: "pulled", newCommit: res.new_commit, summary: res.summary, plan: parsePlanMeta(res) });
      } else {
        setStep({ kind: "pull_failed", error: res.error || "未知错误" });
      }
    } catch (e) {
      setStep({
        kind: "pull_failed",
        error: e instanceof Error ? e.message : String(e),
      });
    }
  };

  const pollUpdateJob = (jobId: string, plan: UpdatePlanMeta) => {
    const pollToken = jobPollTokenRef.current + 1;
    jobPollTokenRef.current = pollToken;
    let stopped = false;
    let failures = 0;
    const poll = async () => {
      if (stopped || jobPollTokenRef.current !== pollToken) return;
      try {
        const job: UpdateJobStatus = await getUpdateJob(jobId);
        failures = 0;
        const logs = job.logs || [];
        if (job.status === "succeeded") {
          stopped = true;
          setStep({
            kind: "pulled",
            newCommit: job.new_commit ?? null,
            summary: job.summary || "更新任务已完成。",
            plan,
          });
          return;
        }
        if (job.status === "failed") {
          stopped = true;
          setStep({
            kind: "pull_failed",
            error: [job.error || "更新任务失败", ...logs.slice(-16)].join("\n"),
          });
          return;
        }
        setStep({
          kind: "job_running",
          jobId,
          status: job.status || "running",
          logs,
          plan,
        });
      } catch (e) {
        failures += 1;
        if (failures >= 5) {
          stopped = true;
          setStep({
            kind: "pulled",
            newCommit: null,
            summary: "更新任务已启动，服务可能正在重启；请稍后刷新页面重新检查版本。",
            plan,
          });
          return;
        }
      }
      if (!stopped && jobPollTokenRef.current === pollToken) {
        window.setTimeout(poll, 2000);
      }
    };
    window.setTimeout(poll, 1200);
  };

  const doRestart = async () => {
    if (!window.confirm("确认重启应用？页面将在 5 秒后自动刷新。")) return;
    try {
      await restartApp();
      setStep({ kind: "restarting", countdown: 5 });
      let count = 5;
      timerRef.current = setInterval(() => {
        count -= 1;
        if (count <= 0) {
          if (timerRef.current) clearInterval(timerRef.current);
          window.location.reload();
        } else {
          setStep({ kind: "restarting", countdown: count });
        }
      }, 1000);
    } catch (e) {
      setStep({
        kind: "pull_failed",
        error: e instanceof Error ? e.message : String(e),
      });
    }
  };

  const doPrimaryAction = async (plan: UpdatePlanMeta) => {
    if (plan.actionRequired === "restart") {
      await doRestart();
      return;
    }
    if (plan.manualCommand || plan.actionRequired === "manual" || plan.actionRequired === "unsupported") {
      if (plan.manualCommand && navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(plan.manualCommand);
        window.alert("服务器命令已复制。");
      } else {
        window.alert("请按弹窗中的命令在服务器上手动执行。");
      }
      return;
    }
    await doPull();
  };

  const isActionable =
    step?.kind === "has_update" ||
    step?.kind === "cannot_check" ||
    step?.kind === "pulled" ||
    step?.kind === "pull_failed" ||
    step?.kind === "check_failed";
  const frontendUpdateMessage = getFrontendUpdateMessage();
  const isCheckingFrontendUpdate = frontendUpdateState === "checking";

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="dialog-center siri-glow-soft w-[calc(100vw-1.5rem)] max-w-md overflow-y-auto border-primary/45 shadow-2xl shadow-primary/10 ring-1 ring-primary/35">
        <DialogHeader>
          <DialogTitle>检查更新</DialogTitle>
          <DialogDescription>
            {step?.kind === "checking" && "正在检查远程仓库..."}
            {step?.kind === "up_to_date" && "当前已是最新版本"}
            {step?.kind === "cannot_check" && "无法自动检查更新"}
            {step?.kind === "has_update" && describeUpdateState(step.plan, step.ahead)}
            {step?.kind === "pulling" && "正在应用更新计划..."}
            {step?.kind === "job_running" && "更新任务正在执行"}
            {step?.kind === "pulled" && "更新计划已执行"}
            {step?.kind === "pull_failed" && "拉取失败"}
            {step?.kind === "check_failed" && "检查失败"}
            {step?.kind === "restarting" && "正在重启应用..."}
            {!step && "准备检查更新"}
          </DialogDescription>
        </DialogHeader>

        {/* 内容区 */}
        <div className="min-h-[80px]">
          <div className="mb-3 flex items-center justify-between gap-3 rounded-md border border-primary/20 bg-primary/5 px-3 py-2 text-xs">
            <span className="text-muted-foreground">当前应用版本</span>
            <code className="rounded bg-background px-2 py-1 font-mono text-foreground">{APP_VERSION_LABEL}</code>
          </div>

          {step?.kind === "checking" && (
            <div className="flex items-center gap-3 text-muted-foreground">
              <Loader2 className="h-5 w-5 animate-spin" />
              <span className="text-sm">正在检查目标分支...</span>
            </div>
          )}

          {step?.kind === "up_to_date" && (
            <div className="flex items-center gap-3 text-success">
              <CheckCircle2 className="h-5 w-5" />
              <div className="text-sm space-y-1">
                <p>当前版本 <code className="bg-muted px-1 rounded">{step.commit}</code></p>
                <p className="text-muted-foreground">无需更新</p>
              </div>
            </div>
          )}

          {step?.kind === "cannot_check" && (
            <div className="space-y-2 text-sm">
              <div className="flex items-center gap-2 text-warning">
                <AlertCircle className="h-5 w-5" />
                <span>容器内无法自动检查更新</span>
              </div>
              <p className="text-muted-foreground">
                {step.plan.planDetail || "当前运行环境无法在容器内比对 Git 远程差异。"}
              </p>
              {step.plan.manualCommand && (
                <div className="rounded-md border bg-background px-3 py-2">
                  <p className="mb-1 text-xs text-muted-foreground">请在服务器执行</p>
                  <pre className="text-xs overflow-x-auto font-mono">{step.plan.manualCommand}</pre>
                </div>
              )}
            </div>
          )}

          {step?.kind === "has_update" && (
            <div className="space-y-2 text-sm">
              <div className="flex items-center gap-2 text-warning">
                <AlertCircle className="h-5 w-5" />
                {step.ahead > 0 ? (
                  <span>远程有 {step.ahead} 个新 commit</span>
                ) : (
                  <span>容器内无法直接检查远程 commit</span>
                )}
              </div>
              {step.plan.planLabel && (
                <p className="rounded-md border bg-background px-3 py-2">
                  {step.plan.planLabel}
                </p>
              )}
              {step.plan.planDetail && (
                <p className="text-muted-foreground">{step.plan.planDetail}</p>
              )}
              <div className="rounded-md bg-muted px-3 py-2 font-mono text-xs space-y-1">
                {(step.current !== "?" || step.remote !== "?") ? (
                  <>
                    <p>当前: {step.current}</p>
                    <p>远程: {step.remote}</p>
                  </>
                ) : (
                  <p>代码版本: 请在宿主机查看</p>
                )}
                {step.plan.runtimeMode && <p>运行模式: {step.plan.runtimeMode}</p>}
                {step.plan.branch && <p>目标分支: {(step.plan.remote || "origin")}/{step.plan.branch}</p>}
                {step.plan.updateExecutor && <p>执行器: {step.plan.updateExecutor}</p>}
              </div>
              {step.plan.components.length > 0 && (
                <div className="rounded-md border bg-background px-3 py-2">
                  <p className="mb-1 text-xs text-muted-foreground">
                    {step.changedFiles.length > 0 ? "变更组件" : "建议更新方式"}
                  </p>
                  <div className="flex flex-wrap gap-1">
                    {step.plan.components.map((name) => (
                      <span key={name} className="rounded bg-muted px-2 py-0.5 text-xs">{name}</span>
                    ))}
                  </div>
                </div>
              )}
              {(step.plan.requiresBackup || step.plan.requiresFullUpdate) && (
                <div className="rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-xs space-y-1">
                  {step.plan.requiresBackup && <p>建议先备份数据后再执行更新。</p>}
                  {step.plan.requiresFullUpdate && <p>该更新需要完整更新流程，耗时会更长。</p>}
                </div>
              )}
              {step.plan.manualCommand && (
                <div className="rounded-md border bg-background px-3 py-2">
                  <p className="mb-1 text-xs text-muted-foreground">服务器命令</p>
                  <pre className="text-xs overflow-x-auto font-mono">{step.plan.manualCommand}</pre>
                </div>
              )}
              {step.changedFiles.length > 0 && (
                <div className="rounded-md border bg-background px-3 py-2">
                  <p className="mb-1 text-xs text-muted-foreground">
                    本次可能变更 {step.changedFiles.length} 个文件
                  </p>
                  <div className="max-h-24 space-y-0.5 overflow-y-auto font-mono text-xs">
                    {step.changedFiles.slice(0, 20).map((file) => (
                      <p key={file} className="truncate">{file}</p>
                    ))}
                    {step.changedFiles.length > 20 && (
                      <p className="text-muted-foreground">...</p>
                    )}
                  </div>
                </div>
              )}
            </div>
          )}

          {step?.kind === "pulling" && (
            <div className="flex items-center gap-3 text-muted-foreground">
              <Loader2 className="h-5 w-5 animate-spin" />
              <span className="text-sm">正在执行更新计划，请稍候...</span>
            </div>
          )}

          {step?.kind === "job_running" && (
            <div className="space-y-3 text-sm">
              <div className="flex items-center gap-3 text-muted-foreground">
                <Loader2 className="h-5 w-5 animate-spin" />
                <span>任务 {step.jobId} · {step.status}</span>
              </div>
              <div className="rounded-md border bg-background px-3 py-2">
                <p className="mb-1 text-xs text-muted-foreground">
                  {(step.plan.remote || "origin")}/{step.plan.branch || "main"} · 最近日志
                </p>
                <pre className="max-h-48 overflow-y-auto whitespace-pre-wrap text-xs leading-relaxed">
                  {step.logs.length ? step.logs.slice(-40).join("\n") : "等待 updater 输出..."}
                </pre>
              </div>
            </div>
          )}

          {step?.kind === "pulled" && (
            <div className="flex items-center gap-3 text-success">
              <CheckCircle2 className="h-5 w-5" />
              <div className="text-sm space-y-1">
                {step.newCommit ? (
                  <p>已更新到 <code className="bg-muted px-1 rounded">{step.newCommit}</code></p>
                ) : (
                  <p>更新计划已执行</p>
                )}
                {step.summary && (
                  <p className="text-muted-foreground">{step.summary}</p>
                )}
                {step.plan.planDetail && (
                  <p className="text-muted-foreground">{step.plan.planDetail}</p>
                )}
                {step.plan.actionRequired === "restart" ? (
                  <p className="text-warning">需要重启应用才能生效</p>
                ) : (
                  <p className="text-warning">
                    更新已提交，请按提示刷新页面或等待服务完成重启。
                  </p>
                )}
                {step.plan.manualCommand && (
                  <pre className="rounded bg-muted px-3 py-2 text-xs overflow-x-auto font-mono text-foreground">
                    {step.plan.manualCommand}
                  </pre>
                )}
              </div>
            </div>
          )}

          {(step?.kind === "pull_failed" || step?.kind === "check_failed") && (
            <div className="flex items-start gap-3 text-destructive">
              <AlertCircle className="h-5 w-5 mt-0.5" />
              <div className="text-sm space-y-1">
                <p>错误信息：</p>
                <pre className="rounded bg-muted px-3 py-2 text-xs overflow-x-auto">
                  {step.error}
                </pre>
              </div>
            </div>
          )}

          {step?.kind === "restarting" && (
            <div className="flex items-center gap-3 text-muted-foreground">
              <Loader2 className="h-5 w-5 animate-spin" />
              <span className="text-sm">
                正在重启，{step.countdown} 秒后自动刷新页面...
              </span>
            </div>
          )}

          <div className="mt-4 rounded-md border bg-background px-3 py-3 text-sm">
            <div className="flex flex-col gap-3 sm:flex-row sm:items-center sm:justify-between">
              <div className="min-w-0">
                <p className="font-medium">前端资源</p>
                <p className="mt-1 text-xs text-muted-foreground">
                  当前前端版本 <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-foreground">v{APP_VERSION}</code>
                </p>
              </div>
              <Button
                variant="outline"
                size="sm"
                onClick={() => void doCheckFrontendUpdate()}
                disabled={isCheckingFrontendUpdate || frontendUpdateState === "updating"}
              >
                {isCheckingFrontendUpdate ? (
                  <Loader2 className="mr-1 h-3.5 w-3.5 animate-spin" />
                ) : (
                  <RefreshCw className="mr-1 h-3.5 w-3.5" />
                )}
                {isCheckingFrontendUpdate ? "检查中..." : "检查前端更新"}
              </Button>
            </div>
            {frontendUpdateMessage && (
              <p className={`mt-3 text-xs ${getFrontendUpdateMessageClassName()}`}>
                {frontendUpdateMessage}
              </p>
            )}
          </div>
        </div>

        {/* 按钮区 */}
        {isActionable && (
          <DialogFooter className="gap-2">
            {(step?.kind === "check_failed" || step?.kind === "pull_failed") && (
              <Button variant="outline" size="sm" onClick={doCheck}>
                <RefreshCw className="mr-1 h-3.5 w-3.5" />
                重新检查
              </Button>
            )}
            {step?.kind === "cannot_check" && (
              <>
                <Button variant="outline" size="sm" onClick={doCheck}>
                  <RefreshCw className="mr-1 h-3.5 w-3.5" />
                  重新检查
                </Button>
                {step.plan.manualCommand && (
                  <Button size="sm" onClick={() => void doPrimaryAction(step.plan)}>
                    复制服务器命令
                  </Button>
                )}
              </>
            )}
            {step?.kind === "has_update" && (
              <Button
                size="sm"
                onClick={() => void doPrimaryAction(step.plan)}
                disabled={!step.plan.canApply && !step.plan.manualCommand}
              >
                {step.plan.actionRequired === "restart" ? (
                  <RotateCcw className="mr-1 h-3.5 w-3.5" />
                ) : (
                  <RefreshCw className="mr-1 h-3.5 w-3.5" />
                )}
                {getPrimaryActionLabel(step.plan)}
              </Button>
            )}
            {step?.kind === "pulled" && (
              <>
                <Button variant="outline" size="sm" onClick={doCheck}>
                  再次检查
                </Button>
                {!step.plan.runtimeMode || step.plan.actionRequired === "restart" ? (
                  <Button size="sm" onClick={doRestart}>
                    <RotateCcw className="mr-1 h-3.5 w-3.5" />
                    重启应用
                  </Button>
                ) : (
                  <Button size="sm" onClick={() => window.location.reload()}>
                    <RefreshCw className="mr-1 h-3.5 w-3.5" />
                    刷新页面
                  </Button>
                )}
              </>
            )}
          </DialogFooter>
        )}
      </DialogContent>
    </Dialog>
  );
}
