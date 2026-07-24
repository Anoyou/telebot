import { MessageSquarePlus, Trash2 } from "lucide-react";

import type { SystemAgentSession } from "@/api/systemAgent";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export function SessionDrawer({
  sessions,
  activeId,
  onSelect,
  onCreate,
  onDelete,
  open,
  onClose,
}: {
  sessions: SystemAgentSession[];
  activeId: string | null;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onDelete: (id: string) => void;
  open?: boolean;
  onClose?: () => void;
}) {
  const body = (
    <div className="flex h-full flex-col">
      <div className="flex items-center justify-between border-b px-3 py-3">
        <div className="text-sm font-medium">会话</div>
        <Button type="button" size="sm" variant="outline" onClick={onCreate}>
          <MessageSquarePlus className="mr-1 h-4 w-4" />
          新建
        </Button>
      </div>
      <div className="flex-1 space-y-1 overflow-y-auto p-2">
        {sessions.length === 0 ? (
          <p className="px-2 py-6 text-center text-sm text-muted-foreground">暂无会话</p>
        ) : (
          sessions.map((s) => (
            <div
              key={s.id}
              className={cn(
                "group flex items-center gap-1 rounded-lg px-2 py-2 text-sm hover:bg-muted/60",
                activeId === s.id && "bg-muted",
              )}
            >
              <button
                type="button"
                className="min-w-0 flex-1 truncate text-left"
                onClick={() => {
                  onSelect(s.id);
                  onClose?.();
                }}
              >
                {s.title || "未命名对话"}
              </button>
              <button
                type="button"
                className="shrink-0 rounded p-1 text-muted-foreground opacity-0 hover:text-destructive group-hover:opacity-100"
                title="删除会话"
                onClick={() => onDelete(s.id)}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </button>
            </div>
          ))
        )}
      </div>
    </div>
  );

  return (
    <>
      {/* 桌面侧栏 */}
      <aside data-assistant-session-anchor className="hidden w-64 shrink-0 border-r bg-card/40 md:block">{body}</aside>
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
