import { Bot, User, Wrench } from "lucide-react";

import type { SystemAgentMessage } from "@/api/systemAgent";
import { cn } from "@/lib/utils";

export type LiveBubble = {
  id: string;
  role: "user" | "assistant" | "tool" | "system";
  text: string;
  pending?: boolean;
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
}: {
  messages: SystemAgentMessage[];
  live?: LiveBubble[];
}) {
  const items: LiveBubble[] = [
    ...messages.map(
      (m): LiveBubble => ({
        id: `m-${m.id}`,
        role: (m.role === "user" || m.role === "assistant" || m.role === "tool"
          ? m.role
          : "system") as LiveBubble["role"],
        text: messageText(m),
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
            <div
              className={cn(
                "max-w-[85%] whitespace-pre-wrap rounded-2xl px-3 py-2 text-sm leading-relaxed",
                isUser && "bg-primary text-primary-foreground",
                !isUser && !isTool && "bg-muted",
                isTool && "border border-dashed bg-background text-xs text-muted-foreground",
                item.pending && "opacity-70",
              )}
            >
              {item.text || (item.pending ? "思考中…" : "")}
            </div>
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
