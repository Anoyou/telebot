import { useCallback, useEffect, useRef, useState } from "react";
import { RefreshCw, RotateCcw, CheckCircle2, AlertCircle, Copy, ChevronDown } from "lucide-react";
import { Spinner } from "@/components/ui/misc";

import {
  Dialog,
  DialogContent,
  DialogHeader,
  DialogTitle,
  DialogDescription,
  DialogFooter,
} from "@/components/ui/dialog";
import { Button } from "@/components/ui/button";
import { Label } from "@/components/ui/label";
import { Select } from "@/components/ui/select";
import {
  checkUpdate,
  getSystemSettings,
  getUpdateJob,
  getUpdateTargetOptions,
  patchSystemSettings,
  pullUpdate,
  restartApp,
} from "@/api/system";
import type { AppUpdateTarget } from "@/api/system";
import type {
  CheckUpdateResult,
  PullUpdateResult,
  UpdateJobStatus,
} from "@/api/types";
import { APP_VERSION, APP_VERSION_LABEL } from "@/lib/version";
import {
  clearActiveUpdateJob,
  getUpdateJobRetryDelay,
  loadActiveUpdateJob,
  saveActiveUpdateJob,
} from "@/lib/updateJobPersistence";

type UpdateActionRequired =
  | "none"
  | "docs_only"
  | "frontend"
  | "backend"
  | "mixed"
  | "updater"
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
  services: string[];
  requiresFullUpdate: boolean;
  requiresBackup: boolean;
  requiresMigration: boolean;
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
  | { kind: "has_update"; current: string; remote: string; currentVersion: string; targetVersion: string; ahead: number; changedFiles: string[]; plan: UpdatePlanMeta }
  | { kind: "pulling" }
  | { kind: "job_running"; jobId: string; status: string; logs: string[]; plan: UpdatePlanMeta; progress: number; phase: string; detail: string | null }
  | { kind: "pulled"; newCommit: string | null; summary: string | null; plan: UpdatePlanMeta }
  | { kind: "pull_failed"; error: string; progress?: number; phase?: string; detail?: string | null }
  | { kind: "check_failed"; error: string }
  | { kind: "restarting"; countdown: number };

interface UpdateDialogProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

function getUpdateJobStorage() {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

function UpdateProgress({
  progress,
  phase,
  detail,
  failed = false,
}: {
  progress: number;
  phase: string;
  detail?: string | null;
  failed?: boolean;
}) {
  const normalized = Math.max(0, Math.min(100, progress));
  return (
    <div className="space-y-2">
      <div className="flex items-center justify-between gap-3 text-xs">
        <span className={failed ? "font-medium text-destructive" : "font-medium text-foreground"}>{phase}</span>
        <span className="font-mono tabular-nums text-muted-foreground">{normalized}%</span>
      </div>
      <div
        className="h-2 overflow-hidden rounded-full bg-muted"
        role="progressbar"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={normalized}
        aria-label={phase}
      >
        <div
          className={`h-full rounded-full transition-[width] duration-500 ease-out ${failed ? "bg-destructive" : "bg-primary"}`}
          style={{ width: `${normalized}%` }}
        />
      </div>
      {detail ? <p className="text-xs text-muted-foreground">{detail}</p> : null}
    </div>
  );
}

export function UpdateDialog({ open, onOpenChange }: UpdateDialogProps) {
  const [step, setStep] = useState<Step | null>(null);
  const [updateRemote, setUpdateRemote] = useState("origin");
  const [updateBranch, setUpdateBranch] = useState("main");
  const [remoteOptions, setRemoteOptions] = useState(["origin"]);
  const [branchOptions, setBranchOptions] = useState(["main"]);
  const [targetsLoading, setTargetsLoading] = useState(false);
  const [targetSaving, setTargetSaving] = useState(false);
  const [errorCopied, setErrorCopied] = useState(false);
  const [planExpanded, setPlanExpanded] = useState(false);
  const timerRef = useRef<ReturnType<typeof setInterval>>();
  const jobPollTokenRef = useRef(0);
  const checkTokenRef = useRef(0);
  const dialogGenerationRef = useRef(0);
  const targetOptionsTokenRef = useRef(0);

  const normalizeAction = (raw: CheckUpdateResult["action_required"]): UpdateActionRequired => {
    return typeof raw === "string" ? raw : "none";
  };

  const parsePlanMeta = (res: CheckUpdateResult | PullUpdateResult): UpdatePlanMeta => ({
    runtimeMode: res.runtime_mode ?? null,
    actionRequired: normalizeAction(res.action_required),
    planLabel: res.plan_label ?? null,
    planDetail: res.plan_detail ?? null,
    components: res.components ?? [],
    services: res.services ?? [],
    requiresFullUpdate: Boolean(res.requires_full_update),
    requiresBackup: Boolean(res.requires_backup),
    requiresMigration: Boolean(res.requires_migration),
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
      case "updater":
        return "更新在线更新器";
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

  const loadTargetOptions = useCallback(async (remote: string, preferredBranch?: string) => {
    const token = targetOptionsTokenRef.current + 1;
    targetOptionsTokenRef.current = token;
    setTargetsLoading(true);
    try {
      const result = await getUpdateTargetOptions(remote);
      if (targetOptionsTokenRef.current !== token) return;
      const remotes = Array.from(new Set([...(result.remotes || []), remote].filter(Boolean)));
      const discoveredBranches = result.branches || [];
      const branches = discoveredBranches.length
        ? Array.from(new Set(discoveredBranches))
        : [preferredBranch || "main"];
      const selectedRemote = result.remote && remotes.includes(result.remote) ? result.remote : remote;
      const selectedBranch = preferredBranch && branches.includes(preferredBranch)
        ? preferredBranch
        : (branches[0] || "main");
      setRemoteOptions(remotes.length ? remotes : [remote || "origin"]);
      setBranchOptions(branches.length ? branches : [selectedBranch]);
      setUpdateRemote(selectedRemote || "origin");
      setUpdateBranch(selectedBranch);
    } catch {
      if (targetOptionsTokenRef.current !== token) return;
      setRemoteOptions((current) => Array.from(new Set([...current, remote].filter(Boolean))));
      if (preferredBranch) {
        setBranchOptions((current) => Array.from(new Set([...current, preferredBranch])));
      }
    } finally {
      if (targetOptionsTokenRef.current === token) setTargetsLoading(false);
    }
  }, []);

  const pollUpdateJob = useCallback((jobId: string, plan: UpdatePlanMeta) => {
    const pollToken = jobPollTokenRef.current + 1;
    jobPollTokenRef.current = pollToken;
    let stopped = false;
    let failures = 0;
    const poll = async () => {
      if (stopped || jobPollTokenRef.current !== pollToken) return;
      try {
        const job: UpdateJobStatus = await getUpdateJob(jobId);
        if (!job.ok && ["unknown", "unsupported"].includes(job.status)) {
          throw new Error(job.error || "暂时无法读取更新任务");
        }
        failures = 0;
        const logs = job.logs || [];
        if (job.status === "succeeded") {
          stopped = true;
          clearActiveUpdateJob(getUpdateJobStorage());
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
          clearActiveUpdateJob(getUpdateJobStorage());
          setStep({
            kind: "pull_failed",
            error: [job.error || "更新任务失败", ...logs.slice(-16)].join("\n"),
            progress: job.progress ?? 0,
            phase: job.phase || "更新失败",
            detail: job.detail ?? null,
          });
          return;
        }
        setStep({
          kind: "job_running",
          jobId,
          status: job.status || "running",
          logs,
          plan,
          progress: job.progress ?? 0,
          phase: job.phase || "更新中",
          detail: job.detail ?? null,
        });
      } catch {
        failures += 1;
        setStep((current) => {
          if (current?.kind !== "job_running" || current.jobId !== jobId) return current;
          return {
            ...current,
            status: "reconnecting",
            phase: "等待服务恢复",
            detail: `更新期间服务暂时不可用，正在第 ${failures} 次重连；任务仍由 updater 后台执行。`,
          };
        });
      }
      if (!stopped && jobPollTokenRef.current === pollToken) {
        const retryDelay = getUpdateJobRetryDelay(failures);
        window.setTimeout(poll, retryDelay);
      }
    };
    window.setTimeout(poll, 1_200);
  }, []);

  // 打开时优先恢复未完成任务，否则自动检查更新。
  const doCheck = useCallback(async (target: AppUpdateTarget) => {
    const checkToken = checkTokenRef.current + 1;
    checkTokenRef.current = checkToken;
    setStep((current) => current ?? { kind: "checking" });
    try {
      const res: CheckUpdateResult = await checkUpdate(target);
      if (checkTokenRef.current !== checkToken) return;
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
          currentVersion: res.current_version || APP_VERSION,
          targetVersion: res.target_version || "未知",
          ahead: res.ahead,
          changedFiles: res.changed_files ?? [],
          plan: parsePlanMeta(res),
        });
      }
    } catch (e) {
      if (checkTokenRef.current !== checkToken) return;
      setStep({
        kind: "check_failed",
        error: e instanceof Error ? e.message : String(e),
      });
    }
  }, []);

  useEffect(() => {
    if (open) {
      const dialogGeneration = dialogGenerationRef.current + 1;
      dialogGenerationRef.current = dialogGeneration;
      const activeJob = loadActiveUpdateJob<UpdatePlanMeta>(getUpdateJobStorage());
      if (activeJob) {
        const remote = activeJob.plan.remote || "origin";
        const branch = activeJob.plan.branch || "main";
        setUpdateRemote(remote);
        setUpdateBranch(branch);
        void loadTargetOptions(remote, branch);
        setStep({
          kind: "job_running",
          jobId: activeJob.jobId,
          status: "reconnecting",
          logs: [],
          plan: activeJob.plan,
          progress: 0,
          phase: "恢复更新任务",
          detail: "正在重新连接 updater 并读取最新进度。",
        });
        pollUpdateJob(activeJob.jobId, activeJob.plan);
      } else {
        void (async () => {
          try {
            const settings = await getSystemSettings();
            if (dialogGenerationRef.current !== dialogGeneration) return;
            const target = settings.app_update_target ?? { remote: "origin", branch: "main" };
            setUpdateRemote(target.remote || "origin");
            setUpdateBranch(target.branch || "main");
            void loadTargetOptions(target.remote || "origin", target.branch || "main");
            await doCheck(target);
          } catch {
            if (dialogGenerationRef.current !== dialogGeneration) return;
            await doCheck({ remote: "origin", branch: "main" });
          }
        })();
      }
    } else {
      dialogGenerationRef.current += 1;
      setStep(null);
      setPlanExpanded(false);
      setErrorCopied(false);
      jobPollTokenRef.current += 1;
      checkTokenRef.current += 1;
      targetOptionsTokenRef.current += 1;
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = undefined;
      }
    }
    return () => {
      dialogGenerationRef.current += 1;
      jobPollTokenRef.current += 1;
      checkTokenRef.current += 1;
      targetOptionsTokenRef.current += 1;
      if (timerRef.current) {
        clearInterval(timerRef.current);
        timerRef.current = undefined;
      }
    };
  }, [open, doCheck, loadTargetOptions, pollUpdateJob]);

  const doPull = async () => {
    setStep({ kind: "pulling" });
    try {
      const activePlan = step?.kind === "has_update" ? step.plan : null;
      const res: PullUpdateResult = await pullUpdate({
        remote: activePlan?.remote || updateRemote,
        branch: activePlan?.branch || updateBranch,
      });
      if (res.success) {
        const responsePlan = parsePlanMeta(res);
        const plan = activePlan && responsePlan.components.length === 0 ? activePlan : responsePlan;
        if (res.job_id) {
          saveActiveUpdateJob(getUpdateJobStorage(), {
            jobId: res.job_id,
            plan,
            savedAt: Date.now(),
          });
          setStep({
            kind: "job_running",
            jobId: res.job_id,
            status: res.status || "queued",
            logs: [],
            plan,
            progress: 0,
            phase: "排队中",
            detail: "等待 updater 执行",
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

  const saveTargetAndCheck = async () => {
    const remote = updateRemote.trim();
    const branch = updateBranch.trim();
    if (!remote || !branch) {
      setStep({ kind: "check_failed", error: "更新远端和分支不能为空" });
      return;
    }
    setTargetSaving(true);
    setPlanExpanded(false);
    try {
      const settings = await patchSystemSettings({ app_update_target: { remote, branch } });
      const saved = settings.app_update_target ?? { remote, branch };
      setUpdateRemote(saved.remote);
      setUpdateBranch(saved.branch);
      await doCheck(saved);
    } catch (error) {
      setStep({
        kind: "check_failed",
        error: error instanceof Error ? error.message : String(error),
      });
    } finally {
      setTargetSaving(false);
    }
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

  const copyErrorDetails = async (error: string) => {
    try {
      await navigator.clipboard.writeText(error);
      setErrorCopied(true);
      window.setTimeout(() => setErrorCopied(false), 1600);
    } catch {
      window.alert("复制失败，请长按错误内容手动复制。");
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
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="dialog-center siri-glow-soft !flex max-h-[calc(100dvh-1.5rem)] w-[calc(100vw-1.5rem)] max-w-md flex-col overflow-hidden border-primary/45 shadow-2xl shadow-primary/10 ring-1 ring-primary/35">
        <DialogHeader className="shrink-0 pr-6">
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
        <div className="min-h-0 flex-1 overflow-y-auto pr-1">
          <div className="mb-3 flex items-center justify-between gap-3 rounded-md border border-primary/20 bg-primary/5 px-3 py-2 text-xs">
            <span className="text-muted-foreground">当前应用版本</span>
            <code className="rounded bg-background px-2 py-1 font-mono text-foreground">{APP_VERSION_LABEL}</code>
          </div>

          <div className="mb-4 rounded-md border bg-background px-3 py-3">
            <div className="grid min-w-0 grid-cols-[100px_minmax(0,1fr)] gap-3">
              <div className="min-w-0 space-y-1.5">
                <Label htmlFor="app-update-remote">Git 远端</Label>
                <Select
                  id="app-update-remote"
                  value={updateRemote}
                  onChange={(event) => {
                    const nextRemote = event.target.value;
                    setUpdateRemote(nextRemote);
                    void loadTargetOptions(nextRemote, updateBranch);
                  }}
                  disabled={targetsLoading || step?.kind === "pulling" || step?.kind === "job_running"}
                >
                  {remoteOptions.map((remote) => <option key={remote} value={remote}>{remote}</option>)}
                </Select>
              </div>
              <div className="min-w-0 space-y-1.5">
                <Label htmlFor="app-update-branch">检查分支</Label>
                <Select
                  id="app-update-branch"
                  value={updateBranch}
                  onChange={(event) => setUpdateBranch(event.target.value)}
                  disabled={targetsLoading || step?.kind === "pulling" || step?.kind === "job_running"}
                >
                  {branchOptions.map((branch) => <option key={branch} value={branch}>{branch}</option>)}
                </Select>
              </div>
            </div>
            <div className="mt-3 flex items-center justify-between gap-3">
              <p className="min-w-0 text-xs text-muted-foreground">
                检查和应用更新会使用同一目标分支。
              </p>
              <Button
                type="button"
                size="sm"
                className="shrink-0"
                loading={targetSaving || targetsLoading}
                onClick={() => void saveTargetAndCheck()}
                disabled={targetsLoading || targetSaving || step?.kind === "checking" || step?.kind === "pulling" || step?.kind === "job_running"}
              >
                {!targetSaving && !targetsLoading ? <RefreshCw className="mr-1 h-3.5 w-3.5" /> : null}
                {targetsLoading ? "读取分支" : "保存并检查"}
              </Button>
            </div>
          </div>

          {step?.kind === "checking" && (
            <div className="flex items-center gap-3 text-muted-foreground">
              <Spinner size="lg" />
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
              <details
                className="group rounded-md border bg-background"
                open={planExpanded}
                onToggle={(event) => setPlanExpanded(event.currentTarget.open)}
              >
                <summary className="flex min-h-11 cursor-pointer list-none items-center justify-between gap-3 px-3 py-2 [&::-webkit-details-marker]:hidden">
                  <span className="min-w-0">
                    <span className="block text-xs font-semibold text-foreground">更新详情</span>
                    <span className="mt-0.5 block text-[11px] text-muted-foreground">
                      v{step.currentVersion} → v{step.targetVersion} · {step.ahead > 0 ? `${step.ahead} 个新 commit` : "部署待完成"}
                    </span>
                  </span>
                  <ChevronDown className="h-4 w-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-180" />
                </summary>
                <div className="space-y-2 border-t px-3 py-3">
                  {step.plan.planDetail && (
                    <p className="text-muted-foreground">{step.plan.planDetail}</p>
                  )}
                  <div className="rounded-md bg-muted px-3 py-2 font-mono text-xs space-y-1">
                    {(step.current !== "?" || step.remote !== "?") ? (
                      <>
                        <p>当前提交: {step.current}</p>
                        <p>远程提交: {step.remote}</p>
                      </>
                    ) : (
                      <p>代码版本: 请在宿主机查看</p>
                    )}
                    {step.plan.runtimeMode && <p>运行模式: {step.plan.runtimeMode}</p>}
                    {step.plan.branch && <p>目标分支: {(step.plan.remote || "origin")}/{step.plan.branch}</p>}
                    {step.plan.updateExecutor && <p>执行器: {step.plan.updateExecutor}</p>}
                  </div>
                </div>
              </details>
              {step.plan.components.length > 0 && planExpanded && (
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
              {step.plan.services.length > 0 && planExpanded && (
                <div className="rounded-md border border-success/30 bg-success/10 px-3 py-2 text-xs space-y-1">
                  <p>本次仅切换：{step.plan.services.join("、")}</p>
                  {!step.plan.requiresMigration && <p>PostgreSQL / Redis 保持运行，不备份、不迁移。</p>}
                </div>
              )}
              {(step.plan.requiresBackup || step.plan.requiresFullUpdate) && planExpanded && (
                <div className="rounded-md border border-warning/30 bg-warning/10 px-3 py-2 text-xs space-y-1">
                  {step.plan.requiresBackup && <p>检测到数据库迁移，将自动备份后再切换后端。</p>}
                  {step.plan.requiresFullUpdate && <p>该更新需要完整更新流程，耗时会更长。</p>}
                </div>
              )}
              {step.plan.manualCommand && planExpanded && (
                <div className="rounded-md border bg-background px-3 py-2">
                  <p className="mb-1 text-xs text-muted-foreground">服务器命令</p>
                  <pre className="text-xs overflow-x-auto font-mono">{step.plan.manualCommand}</pre>
                </div>
              )}
              {step.changedFiles.length > 0 && planExpanded && (
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
            <div className="space-y-3">
              <div className="flex items-center gap-3 text-muted-foreground">
                <Spinner size="lg" />
                <span className="text-sm">正在创建更新任务...</span>
              </div>
              <UpdateProgress progress={2} phase="准备更新" detail="提交目标远端与分支" />
            </div>
          )}

          {step?.kind === "job_running" && (
            <div className="space-y-3 text-sm">
              <div className="flex items-center gap-3 text-muted-foreground">
                <Spinner size="lg" />
                <span>任务 {step.jobId} · {step.status}</span>
              </div>
              <UpdateProgress progress={step.progress} phase={step.phase} detail={step.detail} />
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
            <div className="flex min-w-0 items-start gap-3 text-destructive">
              <AlertCircle className="mt-0.5 h-5 w-5 shrink-0" />
              <div className="min-w-0 flex-1 space-y-2 text-sm">
                {step.kind === "pull_failed" && step.phase ? (
                  <UpdateProgress
                    progress={step.progress ?? 0}
                    phase={step.phase}
                    detail={step.detail}
                    failed
                  />
                ) : null}
                <div className="flex items-center justify-between gap-3">
                  <p>错误信息：</p>
                  <Button
                    type="button"
                    variant="outline"
                    size="sm"
                    className="h-8 shrink-0 text-foreground"
                    onClick={() => void copyErrorDetails(step.error)}
                  >
                    {errorCopied ? <CheckCircle2 className="mr-1 h-3.5 w-3.5" /> : <Copy className="mr-1 h-3.5 w-3.5" />}
                    {errorCopied ? "已复制" : "复制错误"}
                  </Button>
                </div>
                <pre className="max-h-72 max-w-full overflow-auto whitespace-pre-wrap break-words rounded bg-muted px-3 py-2 text-xs">
                  {step.error}
                </pre>
              </div>
            </div>
          )}

          {step?.kind === "restarting" && (
            <div className="flex items-center gap-3 text-muted-foreground">
              <Spinner size="lg" />
              <span className="text-sm">
                正在重启，{step.countdown} 秒后自动刷新页面...
              </span>
            </div>
          )}

        </div>

        {/* 按钮区 */}
        {isActionable && (
          <DialogFooter className="shrink-0 gap-2">
            {(step?.kind === "check_failed" || step?.kind === "pull_failed") && (
              <Button variant="outline" size="sm" onClick={() => void doCheck({ remote: updateRemote, branch: updateBranch })}>
                <RefreshCw className="mr-1 h-3.5 w-3.5" />
                重新检查
              </Button>
            )}
            {step?.kind === "cannot_check" && (
              <>
                <Button variant="outline" size="sm" onClick={() => void doCheck({ remote: updateRemote, branch: updateBranch })}>
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
                <Button variant="outline" size="sm" onClick={() => void doCheck({ remote: updateRemote, branch: updateBranch })}>
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
