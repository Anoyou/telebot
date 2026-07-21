import { useState } from "react";
import { AlertTriangle, Check, RefreshCw, X } from "lucide-react";
import { toast } from "sonner";

import {
  confirmSystemAgentAction,
  rejectSystemAgentAction,
  retrySystemAgentRuntimeSync,
  type SystemAgentAction,
} from "@/api/systemAgent";
import { SecretInput } from "@/components/assistant/SecretInput";
import { Button } from "@/components/ui/button";
import { getErrMsg } from "@/lib/api";
import { cn } from "@/lib/utils";

export function ActionCard({
  action: initial,
  onUpdated,
}: {
  action: SystemAgentAction;
  onUpdated?: (action: SystemAgentAction) => void;
}) {
  const [action, setAction] = useState(initial);
  const [busyAction, setBusyAction] = useState<"confirm" | "reject" | "retry" | null>(null);
  const busy = busyAction !== null;
  const pending = action.status === "pending";
  const dangerous = action.risk === "dangerous";
  const syncFailed = action.runtime_sync_status === "failed";

  const apply = (next: SystemAgentAction) => {
    setAction(next);
    onUpdated?.(next);
  };

  const onConfirm = async () => {
    setBusyAction("confirm");
    try {
      const res = await confirmSystemAgentAction(action.id);
      if (res.action) apply(res.action);
      if (res.ok) {
        toast.success(res.already_final ? "操作已完成" : "已确认并执行");
      } else {
        toast.error(res.error_message || "执行失败");
      }
    } catch (e) {
      toast.error(getErrMsg(e));
    } finally {
      setBusyAction(null);
    }
  };

  const onReject = async () => {
    setBusyAction("reject");
    try {
      const next = await rejectSystemAgentAction(action.id);
      apply(next);
      toast.message("已拒绝该操作");
    } catch (e) {
      toast.error(getErrMsg(e));
    } finally {
      setBusyAction(null);
    }
  };

  const onRetrySync = async () => {
    setBusyAction("retry");
    try {
      const res = await retrySystemAgentRuntimeSync(action.id);
      if (res.action) apply(res.action);
      if (res.ok) toast.success("已重新同步运行时");
      else toast.error(res.error_message || "同步失败");
    } catch (e) {
      toast.error(getErrMsg(e));
    } finally {
      setBusyAction(null);
    }
  };

  return (
    <div
      className={cn(
        "rounded-xl border p-3 text-sm",
        dangerous ? "border-destructive/50 bg-destructive/5" : "border-border bg-muted/40",
      )}
    >
      <div className="mb-1 flex items-start gap-2">
        {dangerous ? <AlertTriangle className="mt-0.5 h-4 w-4 text-destructive" /> : null}
        <div className="min-w-0 flex-1">
          <div className="font-medium">{action.summary || action.tool_name}</div>
          <div className="mt-0.5 text-xs text-muted-foreground">
            {action.tool_name} · {action.status}
            {action.risk === "dangerous" ? " · 危险操作" : ""}
          </div>
        </div>
      </div>

      {action.preview?.warning ? (
        <p className="mb-2 text-xs text-destructive">{String(action.preview.warning)}</p>
      ) : null}
      {action.preview?.note ? (
        <p className="mb-2 text-xs text-muted-foreground">{String(action.preview.note)}</p>
      ) : null}

      {action.error_message ? (
        <p className="mb-2 text-xs text-destructive">{action.error_message}</p>
      ) : null}

      {syncFailed ? (
        <div className="mb-2 rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-1.5 text-xs">
          配置已保存，运行时同步失败
          {action.runtime_sync_error ? `：${action.runtime_sync_error}` : ""}
        </div>
      ) : null}

      {pending && (action.secret_fields?.length || action.tool_name?.startsWith("providers.")) ? (
        <SecretInput
          actionId={action.id}
          fieldNames={
            action.secret_fields?.length
              ? action.secret_fields
              : action.tool_name?.startsWith("providers.")
                ? ["api_key"]
                : undefined
          }
          onDone={() => {
            // 与后端 secret-input 一致：补 Key 后清旧预检错误
            const next = {
              ...action,
              has_secret: true,
              error_code: null,
              error_message: null,
            };
            setAction(next);
            onUpdated?.(next);
          }}
        />
      ) : null}

      <div className="flex flex-wrap gap-2">
        {pending ? (
          <>
            <Button
              type="button"
              size="sm"
              variant={dangerous ? "destructive" : "default"}
              loading={busyAction === "confirm"}
              disabled={busy}
              onClick={() => void onConfirm()}
            >
              {busyAction !== "confirm" ? <Check className="h-3.5 w-3.5" /> : null}
              确认执行
            </Button>
            <Button type="button" size="sm" variant="outline" loading={busyAction === "reject"} disabled={busy} onClick={() => void onReject()}>
              {busyAction !== "reject" ? <X className="h-3.5 w-3.5" /> : null}
              拒绝
            </Button>
          </>
        ) : null}
        {syncFailed ? (
          <Button type="button" size="sm" variant="outline" loading={busyAction === "retry"} disabled={busy} onClick={() => void onRetrySync()}>
            {busyAction !== "retry" ? <RefreshCw className="h-3.5 w-3.5" /> : null}
            重新同步
          </Button>
        ) : null}
      </div>
    </div>
  );
}
