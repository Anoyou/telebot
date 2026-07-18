import { AlertCircle, Bot, RotateCcw, ShieldCheck, User, Wrench } from "lucide-react";

import type {
  SystemAgentAction,
  SystemAgentMessage,
  SystemAgentProviderSwitch,
  SystemAgentToolApproval,
} from "@/api/systemAgent";
import { ActionCard } from "@/components/assistant/ActionCard";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

export type LiveBubble = {
  id: string;
  role: "user" | "assistant" | "tool" | "system" | "action";
  text: string;
  pending?: boolean;
  action?: SystemAgentAction;
  messageId?: number;
  runStatus?: string;
  errorMessage?: string | null;
  retryCount?: number;
  providerSwitch?: SystemAgentProviderSwitch;
  toolApproval?: SystemAgentToolApproval;
};

function messageText(msg: SystemAgentMessage): string {
  const content = msg.content || {};
  if (typeof content.text === "string") return content.text;
  if (msg.role === "tool") {
    const name = String(content.tool_name || "tool");
    const summary = content.result_summary;
    try {
      return `[${name}] ${JSON.stringify(summary)}`;
    } catch {
      return `[${name}]`;
    }
  }
  return "";
}

export function Conversation({
  messages,
  live,
  onActionUpdated,
  onRetryMessage,
  retryingMessageId,
}: {
  messages: SystemAgentMessage[];
  live?: LiveBubble[];
  onActionUpdated?: (action: SystemAgentAction) => void;
  onRetryMessage?: (
    messageId: number,
    fallbackProviderId?: number,
    approvedTools?: string[],
  ) => void;
  retryingMessageId?: number | null;
}) {
  const items: LiveBubble[] = [
    ...messages.map(
      (m): LiveBubble => ({
        id: `m-${m.id}`,
        role: (m.role === "user" || m.role === "assistant" || m.role === "tool"
          ? m.role
          : "system") as LiveBubble["role"],
        text: messageText(m),
        messageId: m.id,
        runStatus: m.run_status,
        errorMessage: m.error_message,
        retryCount: m.retry_count,
        providerSwitch:
          m.usage?.provider_switch && typeof m.usage.provider_switch === "object"
            ? (m.usage.provider_switch as unknown as SystemAgentProviderSwitch)
            : undefined,
        toolApproval:
          m.usage?.tool_approval && typeof m.usage.tool_approval === "object"
            ? (m.usage.tool_approval as unknown as SystemAgentToolApproval)
            : undefined,
      }),
    ),
    ...(live || []),
  ].filter((item) => item.text || item.pending);

  if (items.length === 0) {
    return (
      <div className="flex flex-1 flex-col items-center justify-center gap-2 p-8 text-center text-muted-foreground">
        <Bot className="h-10 w-10 opacity-40" />
        <p className="text-sm">向系统助手提问，例如：</p>
        <ul className="text-sm">
          <li>「交互里有哪些规则？」</li>
          <li>「最近 20 条运行日志里有什么错误？」</li>
          <li>「我今天收入多少？」</li>
        </ul>
      </div>
    );
  }

  return (
    <div className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-3 overflow-y-auto p-4">
      {items.map((item) => {
        const isUser = item.role === "user";
        const isTool = item.role === "tool";
        const isAction = item.role === "action" && item.action;
        const isFailedUser = isUser && item.runStatus === "failed" && item.messageId != null;
        const switchCandidate = item.providerSwitch?.candidates?.[0];
        const approvalTools = item.toolApproval?.tools || [];
        return (
          <div
            key={item.id}
            className={cn("flex gap-2", isUser ? "justify-end" : "justify-start")}
          >
            {!isUser ? (
              <div className="mt-1 shrink-0 rounded-full bg-muted p-1.5">
                {isTool ? <Wrench className="h-3.5 w-3.5" /> : <Bot className="h-3.5 w-3.5" />}
              </div>
            ) : null}
            {isAction && item.action ? (
              <div className="max-w-[85%] min-w-[16rem]">
                <ActionCard action={item.action} onUpdated={onActionUpdated} />
              </div>
            ) : (
              <div className={cn("flex min-w-0 max-w-[85%] flex-col gap-1", isUser && "items-end")}>
                <div
                  className={cn(
                    "max-w-full break-words whitespace-pre-wrap rounded-2xl px-3 py-2 text-sm leading-relaxed",
                    isUser && "bg-primary text-primary-foreground",
                    !isUser && !isTool && "bg-muted",
                    isTool && "border border-dashed bg-background text-xs text-muted-foreground",
                    item.pending && "opacity-70",
                  )}
                >
                  {item.text || (item.pending ? "思考中…" : "")}
                </div>
                {isFailedUser ? (
                  <div className="flex max-w-full flex-col items-stretch gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-2.5 py-2 text-xs text-destructive sm:flex-row sm:items-start">
                    <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                    <div className="min-w-0 flex-1">
                      <p className="break-words">{item.errorMessage || "本轮执行失败"}</p>
                      {approvalTools.length ? (
                        <p className="mt-1 break-words text-foreground/75">
                          将开放：{approvalTools.map((tool) => tool.name).join("、")}
                        </p>
                      ) : null}
                      {item.retryCount ? <p className="mt-1 opacity-70">已重试 {item.retryCount} 次</p> : null}
                    </div>
                    <div className="flex shrink-0 justify-end gap-1.5 self-end sm:self-auto">
                      {approvalTools.length ? (
                        <Button
                          type="button"
                          size="sm"
                          className="h-7 max-w-48 px-2 text-xs"
                          title={`批准调用：${approvalTools.map((tool) => tool.name).join("、")}`}
                          disabled={retryingMessageId != null}
                          onClick={() =>
                            onRetryMessage?.(
                              item.messageId!,
                              switchCandidate?.provider_id,
                              approvalTools.map((tool) => tool.name),
                            )
                          }
                        >
                          <ShieldCheck className="mr-1 h-3 w-3" />
                          {switchCandidate
                            ? `批准并改用 ${switchCandidate.provider_name}`
                            : "批准调用"}
                        </Button>
                      ) : null}
                      {switchCandidate && !approvalTools.length ? (
                        <Button
                          type="button"
                          size="sm"
                          className="h-7 max-w-48 px-2 text-xs"
                          title={`改用 ${switchCandidate.provider_name} · ${switchCandidate.model}`}
                          disabled={retryingMessageId != null}
                          onClick={() =>
                            onRetryMessage?.(
                              item.messageId!,
                              switchCandidate.provider_id,
                              approvalTools.map((tool) => tool.name),
                            )
                          }
                        >
                          <span className="truncate">改用 {switchCandidate.provider_name}</span>
                        </Button>
                      ) : null}
                      {!approvalTools.length ? (
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          className="h-7 px-2 text-xs text-foreground"
                          disabled={retryingMessageId != null}
                          onClick={() => onRetryMessage?.(item.messageId!)}
                        >
                          <RotateCcw className={cn("mr-1 h-3 w-3", retryingMessageId === item.messageId && "animate-spin")} />
                          {retryingMessageId === item.messageId ? "重试中" : "重试本轮"}
                        </Button>
                      ) : null}
                    </div>
                  </div>
                ) : null}
              </div>
            )}
            {isUser ? (
              <div className="mt-1 shrink-0 rounded-full bg-primary/10 p-1.5">
                <User className="h-3.5 w-3.5" />
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
