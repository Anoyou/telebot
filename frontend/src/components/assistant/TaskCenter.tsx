import { useMemo, useState } from "react";
import {
  ChevronDown,
  ChevronUp,
  CirclePause,
  CirclePlay,
  ListTodo,
  Pencil,
  Trash2,
  X,
} from "lucide-react";

import type {
  SystemAgentQueueItem,
  SystemAgentRun,
  SystemAgentSession,
} from "@/api/systemAgent";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { sortSystemAgentQueue, taskCenterVisibleRuns } from "./taskCenterState";

const DISMISSED_RUNS_KEY = "telepilot.system-agent.dismissed-task-runs.v1";

function readDismissedRunIds(): Set<string> {
  try {
    const value = JSON.parse(window.localStorage.getItem(DISMISSED_RUNS_KEY) || "[]") as unknown;
    if (!Array.isArray(value)) return new Set();
    return new Set(value.filter((item): item is string => typeof item === "string").slice(-200));
  } catch {
    return new Set();
  }
}

function writeDismissedRunIds(values: Set<string>): void {
  try {
    window.localStorage.setItem(DISMISSED_RUNS_KEY, JSON.stringify([...values].slice(-200)));
  } catch {
    // localStorage 不可用时，本页仍可隐藏。
  }
}

const STATUS_LABELS: Record<string, string> = {
  queued: "排队中",
  running: "运行中",
  waiting_input: "等待补充",
  waiting_approval: "等待审批",
  succeeded: "已完成",
  failed: "失败",
  cancelled: "已取消",
  pending: "待执行",
  dispatching: "启动中",
  paused: "已暂停",
};

function statusLabel(status: string): string {
  return STATUS_LABELS[status] || status;
}

function statusClass(status: string): string {
  if (status === "running" || status === "dispatching") {
    return "border-blue-500/30 bg-blue-500/10 text-blue-700 dark:text-blue-300";
  }
  if (status.startsWith("waiting") || status === "paused") {
    return "border-amber-500/30 bg-amber-500/10 text-amber-700 dark:text-amber-300";
  }
  if (status === "failed") {
    return "border-destructive/30 bg-destructive/10 text-destructive";
  }
  if (status === "succeeded") {
    return "border-emerald-500/30 bg-emerald-500/10 text-emerald-700 dark:text-emerald-300";
  }
  return "border-border bg-muted/50 text-muted-foreground";
}

export function TaskCenter({
  sessions,
  runs,
  queue,
  activeSessionId,
  onSelectSession,
  onEditQueueItem,
  onDeleteQueueItem,
  onMoveQueueItem,
  onClearQueue,
  onResumeQueue,
}: {
  sessions: SystemAgentSession[];
  runs: SystemAgentRun[];
  queue: SystemAgentQueueItem[];
  activeSessionId: string | null;
  onSelectSession: (sessionId: string) => void;
  onEditQueueItem: (item: SystemAgentQueueItem, content: string) => void;
  onDeleteQueueItem: (item: SystemAgentQueueItem) => void;
  onMoveQueueItem: (item: SystemAgentQueueItem, direction: -1 | 1) => void;
  onClearQueue: (sessionId: string) => void;
  onResumeQueue: (sessionId: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [editingItem, setEditingItem] = useState<SystemAgentQueueItem | null>(null);
  const [editingContent, setEditingContent] = useState("");
  const [clearSessionId, setClearSessionId] = useState<string | null>(null);
  const [dismissedRunIds, setDismissedRunIds] = useState(readDismissedRunIds);
  const sessionTitles = useMemo(
    () => new Map(sessions.map((session) => [session.id, session.title || "未命名对话"])),
    [sessions],
  );
  const visibleRuns = taskCenterVisibleRuns(runs, dismissedRunIds);
  const currentQueue = activeSessionId
    ? sortSystemAgentQueue(queue.filter((item) => item.session_id === activeSessionId))
    : [];
  const activeSession = sessions.find((session) => session.id === activeSessionId);
  const canManageCurrentQueue = activeSession?.channel !== "bot";
  const editableQueue = currentQueue.filter((item) =>
    item.channel === "web" && ["pending", "paused"].includes(item.status),
  );
  const editableIndexById = new Map(
    editableQueue.map((item, index) => [item.id, index]),
  );
  const attentionCount =
    runs.filter((run) => ["running", "waiting_input", "waiting_approval"].includes(run.status))
      .length
      + visibleRuns.filter((run) => run.status === "failed").length
      + queue.filter((item) => item.status !== "dispatching").length;

  if (visibleRuns.length === 0 && queue.length === 0) return null;

  return (
    <>
    <div className="shrink-0 border-b border-border/60 bg-muted/20">
      <button
        type="button"
        className="flex min-h-10 w-full items-center gap-2 px-3 text-left text-xs hover:bg-muted/40"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
      >
        <ListTodo className="h-4 w-4 text-primary" />
        <span className="font-medium">任务中心</span>
        <Badge variant="outline" className="h-5 px-1.5 text-[10px]">
          {attentionCount}
        </Badge>
        <span className="min-w-0 truncate text-muted-foreground">
          {currentQueue.length > 0
            ? `当前会话还有 ${currentQueue.length} 条队列消息`
            : "跨会话查看运行、等待与失败任务"}
        </span>
        {open ? (
          <ChevronUp className="ml-auto h-4 w-4 shrink-0 text-muted-foreground" />
        ) : (
          <ChevronDown className="ml-auto h-4 w-4 shrink-0 text-muted-foreground" />
        )}
      </button>
      {open ? (
        <div className="grid max-h-[min(22rem,42dvh)] gap-3 overflow-y-auto border-t border-border/50 p-3 lg:grid-cols-2">
          <section>
            <div className="mb-2 flex items-center justify-between">
              <h3 className="text-xs font-medium">运行与等待</h3>
              <span className="text-[10px] text-muted-foreground">{visibleRuns.length} 项</span>
            </div>
            <div className="space-y-1.5">
              {visibleRuns.length === 0 ? (
                <p className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">
                  当前没有运行中的任务。
                </p>
              ) : (
                visibleRuns.map((run) => (
                  <div
                    key={run.id}
                    className={cn(
                      "flex min-h-11 w-full items-center gap-2 rounded-md border px-2.5 py-2 text-left hover:bg-muted/50",
                      run.session_id === activeSessionId && "border-primary/35 bg-primary/5",
                    )}
                  >
                    <button
                      type="button"
                      className="flex min-w-0 flex-1 items-center gap-2 text-left"
                      onClick={() => onSelectSession(run.session_id)}
                    >
                      <Badge variant="outline" className={cn("shrink-0 text-[10px]", statusClass(run.status))}>
                        {statusLabel(run.status)}
                      </Badge>
                      <span className="min-w-0 flex-1">
                        <span className="block truncate text-xs font-medium">
                          {sessionTitles.get(run.session_id) || run.session_id.slice(0, 8)}
                        </span>
                        <span className="block truncate text-[10px] text-muted-foreground">
                          {run.phase || run.kind}
                          {run.error_message ? ` · ${run.error_message}` : ""}
                        </span>
                      </span>
                      {run.elapsed_ms != null ? (
                        <span className="shrink-0 text-[10px] text-muted-foreground">
                          {(run.elapsed_ms / 1000).toFixed(1)}s
                        </span>
                      ) : null}
                    </button>
                    {run.status === "failed" ? (
                      <button
                        type="button"
                        className="grid h-7 w-7 shrink-0 place-items-center rounded-md text-muted-foreground hover:bg-muted hover:text-foreground"
                        title="从任务中心移除，运行记录仍保留"
                        aria-label="从任务中心移除失败任务"
                        onClick={() => {
                          setDismissedRunIds((current) => {
                            const next = new Set(current);
                            next.add(run.id);
                            writeDismissedRunIds(next);
                            return next;
                          });
                        }}
                      >
                        <X className="h-3.5 w-3.5" />
                      </button>
                    ) : null}
                  </div>
                ))
              )}
            </div>
          </section>
          <section>
            <div className="mb-2 flex min-h-7 items-center justify-between gap-2">
              <h3 className="text-xs font-medium">当前会话队列</h3>
              {activeSessionId && currentQueue.length > 0 && canManageCurrentQueue ? (
                <Button
                  type="button"
                  size="sm"
                  variant="ghost"
                  className="h-7 px-2 text-[10px] text-muted-foreground"
                  onClick={() => setClearSessionId(activeSessionId)}
                >
                  <Trash2 className="mr-1 h-3 w-3" />
                  清空
                </Button>
              ) : null}
            </div>
            <div className="space-y-1.5">
              {currentQueue.length === 0 ? (
                <p className="rounded-md border border-dashed p-3 text-xs text-muted-foreground">
                  当前会话没有排队消息。
                </p>
              ) : (
                currentQueue.map((item, index) => {
                  const editableIndex = editableIndexById.get(item.id);
                  return (
                  <div
                    key={item.id}
                    className="grid min-h-12 grid-cols-[1rem_minmax(0,1fr)] items-start gap-x-2 gap-y-1 rounded-md border px-2.5 py-2 sm:grid-cols-[1rem_minmax(0,1fr)_auto] sm:items-center"
                  >
                    {item.status === "paused" ? (
                      <CirclePause className="mt-0.5 h-4 w-4 shrink-0 text-amber-600 sm:mt-0" />
                    ) : (
                      <span className="mt-0.5 w-4 shrink-0 text-center text-[10px] tabular-nums text-muted-foreground sm:mt-0">
                        {index + 1}
                      </span>
                    )}
                    <span className="min-w-0">
                      <span className="line-clamp-2 text-xs leading-4">{item.content}</span>
                      <span className="mt-0.5 block text-[10px] text-muted-foreground">
                        {statusLabel(item.status)}
                        {item.blocked_reason ? ` · ${item.blocked_reason}` : ""}
                      </span>
                    </span>
                    <span className="col-span-2 flex min-w-0 flex-wrap justify-end gap-1 sm:col-span-1 sm:flex-nowrap">
                      {item.channel === "web" &&
                      item.status === "paused" &&
                      activeSessionId ? (
                        <Button
                          type="button"
                          size="icon"
                          variant="ghost"
                          className="h-10 w-auto shrink-0 px-2 active:scale-95 sm:h-9 sm:w-9 sm:px-0"
                          onClick={() => onResumeQueue(activeSessionId)}
                          title="恢复后续任务"
                          aria-label="恢复后续任务"
                        >
                          <CirclePlay className="h-4 w-4" />
                          <span className="text-[11px] sm:sr-only">恢复</span>
                        </Button>
                      ) : null}
                      {item.channel === "web" &&
                      ["pending", "paused"].includes(item.status) ? (
                        <>
                          <Button
                            type="button"
                            size="icon"
                            variant="ghost"
                            className="h-10 w-auto shrink-0 px-2 active:scale-95 sm:h-9 sm:w-9 sm:px-0"
                            onClick={() => {
                              setEditingItem(item);
                              setEditingContent(item.content);
                            }}
                            title="修改这条消息"
                            aria-label="修改这条消息"
                          >
                            <Pencil className="h-3.5 w-3.5" />
                            <span className="text-[11px] sm:sr-only">修改</span>
                          </Button>
                          <button
                            type="button"
                            className="inline-flex h-10 shrink-0 items-center justify-center gap-1 rounded-md px-2 text-[11px] active:scale-95 hover:bg-muted disabled:opacity-30 sm:h-9 sm:w-9 sm:px-0"
                            disabled={editableIndex === 0}
                            onClick={() => onMoveQueueItem(item, -1)}
                            aria-label="提前一位"
                            title="提前一位"
                          >
                            <ChevronUp className="h-3.5 w-3.5" />
                            <span className="sm:sr-only">提前</span>
                          </button>
                          <button
                            type="button"
                            className="inline-flex h-10 shrink-0 items-center justify-center gap-1 rounded-md px-2 text-[11px] active:scale-95 hover:bg-muted disabled:opacity-30 sm:h-9 sm:w-9 sm:px-0"
                            disabled={editableIndex === editableQueue.length - 1}
                            onClick={() => onMoveQueueItem(item, 1)}
                            aria-label="延后一位"
                            title="延后一位"
                          >
                            <ChevronDown className="h-3.5 w-3.5" />
                            <span className="sm:sr-only">延后</span>
                          </button>
                          <Button
                            type="button"
                            size="icon"
                            variant="ghost"
                            className="h-10 w-auto shrink-0 px-2 text-muted-foreground active:scale-95 hover:text-destructive sm:h-9 sm:w-9 sm:px-0"
                            onClick={() => onDeleteQueueItem(item)}
                            title="取消这条"
                            aria-label="取消这条"
                          >
                            <X className="h-4 w-4" />
                            <span className="text-[11px] sm:sr-only">取消</span>
                          </Button>
                        </>
                      ) : null}
                    </span>
                  </div>
                  );
                })
              )}
            </div>
          </section>
        </div>
      ) : null}
    </div>
    <Dialog
      open={editingItem !== null}
      onOpenChange={(value) => {
        if (!value) {
          setEditingItem(null);
          setEditingContent("");
        }
      }}
    >
      <DialogContent className="max-w-xl">
        <DialogHeader>
          <DialogTitle>编辑排队消息</DialogTitle>
          <DialogDescription>
            修改后仍会排在原来的位置，轮到它时才会执行。
          </DialogDescription>
        </DialogHeader>
        <Textarea
          value={editingContent}
          onChange={(event) => setEditingContent(event.target.value)}
          rows={6}
          maxLength={32_000}
          autoFocus
          className="max-h-[45dvh] min-h-32 resize-y"
          placeholder="输入排队消息"
        />
        <div className="text-right text-[10px] tabular-nums text-muted-foreground">
          {editingContent.length} / 32000
        </div>
        <DialogFooter className="gap-2 sm:space-x-0">
          <Button
            type="button"
            variant="outline"
            className="min-h-10 w-full active:scale-95 sm:w-auto"
            onClick={() => setEditingItem(null)}
          >
            取消
          </Button>
          <Button
            type="button"
            className="min-h-10 w-full active:scale-95 sm:w-auto"
            disabled={!editingContent.trim()}
            onClick={() => {
              if (!editingItem) return;
              const content = editingContent.trim();
              if (!content || content === editingItem.content) {
                setEditingItem(null);
                return;
              }
              onEditQueueItem(editingItem, content);
              setEditingItem(null);
              setEditingContent("");
            }}
          >
            保存修改
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
    <Dialog
      open={clearSessionId !== null}
      onOpenChange={(value) => {
        if (!value) setClearSessionId(null);
      }}
    >
      <DialogContent className="max-w-md">
        <DialogHeader>
          <DialogTitle>清空当前会话队列</DialogTitle>
          <DialogDescription>
            尚未开始执行的排队消息会被取消，当前正在运行或等待输入的任务不受影响。
          </DialogDescription>
        </DialogHeader>
        <DialogFooter className="gap-2 sm:space-x-0">
          <Button
            type="button"
            variant="outline"
            className="min-h-10 w-full active:scale-95 sm:w-auto"
            onClick={() => setClearSessionId(null)}
          >
            返回
          </Button>
          <Button
            type="button"
            variant="destructive"
            className="min-h-10 w-full active:scale-95 sm:w-auto"
            onClick={() => {
              if (!clearSessionId) return;
              onClearQueue(clearSessionId);
              setClearSessionId(null);
            }}
          >
            确认清空
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
    </>
  );
}
