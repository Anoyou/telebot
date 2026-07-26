import { Bot, MessageSquarePlus, PanelLeftClose, Trash2 } from "lucide-react";

import type { SystemAgentSession } from "@/api/systemAgent";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export type SessionOriginFilter = "all" | "interactive" | "scheduled";

export function SessionDrawer({
  sessions,
  activeId,
  onSelect,
  onCreate,
  onDelete,
  open,
  onClose,
  desktopCollapsed = false,
  onDesktopCollapse,
  originFilter = "all",
  onOriginFilterChange,
}: {
  sessions: SystemAgentSession[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onDelete: (id: string) => void;
  open?: boolean;
  onClose?: () => void;
  desktopCollapsed?: boolean;
  onDesktopCollapse?: () => void;
  originFilter?: SessionOriginFilter;
  onOriginFilterChange?: (value: SessionOriginFilter) => void;
}) {
  const filtered =
    originFilter === "all"
      ? sessions
      : sessions.filter((s) => (s.origin || "interactive") === originFilter);
  const webSessions = filtered.filter((session) => session.channel !== "bot");
  const botSessions = filtered.filter((session) => session.channel === "bot");

  const renderSession = (session: SystemAgentSession) => {
    const isBotSession = session.channel === "bot";
    return (
      <div
        key={session.id}
        className={cn(
          "group flex items-center gap-1 rounded-lg px-2 py-2 text-sm hover:bg-muted/60",
          activeId === session.id && "bg-muted",
        )}
      >
        <button
          type="button"
          className="min-w-0 flex-1 text-left"
          onClick={() => {
            onSelect(session.id);
            onClose?.();
          }}
        >
          <span className="flex min-w-0 items-center gap-1">
            <span className="truncate">{session.title || (isBotSession ? "Telegram 会话" : "未命名对话")}</span>
            {session.origin === "scheduled" ? (
              <span className="inline-block shrink-0 rounded bg-amber-500/15 px-1 text-[10px] text-amber-700 dark:text-amber-300">
                定时
              </span>
            ) : null}
          </span>
          {isBotSession ? (
            <span className="mt-0.5 block truncate text-[10px] text-muted-foreground">
              TG {session.bot_tg_user_id ?? "未知用户"}
              {session.account_id != null ? ` · 账号 #${session.account_id}` : ""}
            </span>
          ) : null}
        </button>
        {!isBotSession ? (
          <button
            type="button"
            className="shrink-0 rounded p-1 text-muted-foreground opacity-0 hover:text-destructive group-hover:opacity-100"
            title="删除会话"
            onClick={() => onDelete(session.id)}
          >
            <Trash2 className="h-3.5 w-3.5" />
          </button>
        ) : null}
      </div>
    );
  };

  const body = (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between gap-2 border-b px-3 py-3">
        <div className="text-sm font-medium">会话</div>
        <div className="flex items-center gap-1">
          {onDesktopCollapse ? (
            <Button
              type="button"
              size="icon"
              variant="ghost"
              className="hidden h-8 w-8 md:inline-flex"
              onClick={onDesktopCollapse}
              aria-label="收起会话列表"
              title="收起会话列表"
            >
              <PanelLeftClose className="h-4 w-4" />
            </Button>
          ) : null}
          <Button type="button" size="sm" variant="outline" onClick={onCreate}>
            <MessageSquarePlus className="mr-1 h-4 w-4" />
            新建
          </Button>
        </div>
      </div>
      {onOriginFilterChange ? (
        <div className="flex gap-1 border-b px-2 py-2">
          {(
            [
              ["all", "全部"],
              ["interactive", "对话"],
              ["scheduled", "定时"],
            ] as const
          ).map(([value, label]) => (
            <button
              key={value}
              type="button"
              className={cn(
                "rounded-md px-2 py-1 text-xs",
                originFilter === value
                  ? "bg-primary/15 text-primary"
                  : "text-muted-foreground hover:bg-muted/60",
              )}
              onClick={() => onOriginFilterChange(value)}
            >
              {label}
            </button>
          ))}
        </div>
      ) : null}
      <div className="flex-1 space-y-1 overflow-y-auto p-2">
        {filtered.length === 0 ? (
          <p className="px-2 py-6 text-center text-sm text-muted-foreground">暂无会话</p>
        ) : (
          <>
            {webSessions.map(renderSession)}
            {botSessions.length > 0 ? (
              <>
                <div role="separator" className="flex items-center gap-2 px-2 pb-1 pt-3 text-[10px] font-medium text-muted-foreground">
                  <span className="h-px flex-1 bg-border" />
                  <span className="inline-flex items-center gap-1 whitespace-nowrap">
                    <Bot className="h-3 w-3" /> Telegram 会话 {botSessions.length}
                  </span>
                  <span className="h-px flex-1 bg-border" />
                </div>
                {botSessions.map(renderSession)}
              </>
            ) : null}
          </>
        )}
      </div>
    </div>
  );

  return (
    <>
      {/* 桌面侧栏 */}
      <aside
        data-assistant-session-anchor
        className={cn(
          "hidden w-64 shrink-0 border-r bg-card/40",
          desktopCollapsed ? "md:hidden" : "md:block",
        )}
      >
        {body}
      </aside>
      {/* 移动抽屉 */}
      {open ? (
        <div className="fixed bottom-[calc(4.75rem+env(safe-area-inset-bottom))] left-0 right-0 top-[calc(3.25rem+env(safe-area-inset-top))] z-[69] md:hidden">
          <button
            type="button"
            className="absolute inset-0 bg-black/20 animate-in fade-in duration-200"
            aria-label="关闭会话列表"
            onClick={onClose}
          />
          <div className="absolute inset-y-0 left-0 z-[70] w-[min(80vw,18rem)] animate-in rounded-r-2xl border-r border-border/70 bg-card shadow-[0_6px_18px_rgba(15,23,42,0.10)] slide-in-from-left-3 duration-200">
            {body}
          </div>
        </div>
      ) : null}
    </>
  );
}
