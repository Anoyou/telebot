import { memo, useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  Bot,
  BrainCircuit,
  Check,
  Copy,
  Pencil,
  RotateCcw,
  ShieldCheck,
  User,
  Wrench,
  X,
} from "lucide-react";
import ReactMarkdown from "react-markdown";
import rehypeRaw from "rehype-raw";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import remarkGfm from "remark-gfm";
import { toast } from "sonner";

import type {
  SystemAgentAction,
  SystemAgentMessage,
  SystemAgentProviderSwitch,
  SystemAgentToolApproval,
} from "@/api/systemAgent";
import { ActionCard } from "@/components/assistant/ActionCard";
import { ModelRunMeta } from "@/components/ai/ModelRunMeta";
import { RunTrace } from "@/components/assistant/RunTrace";
import { StreamingText } from "@/components/ai/StreamingText";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { systemAgentToolLabel } from "@/lib/systemAgentLabels";
import { cn, formatDateTime } from "@/lib/utils";
import {
  ASSISTANT_HTML_CLASSES,
  extractStyleColor,
  mergeConversationItems,
  resolveAssistantTextColor,
  SAFE_LABELS,
  stabilizeStreamingMarkdown,
  visibleConversationMessages,
} from "./conversationState";

type AssistantHtmlNode = {
  type?: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  children?: AssistantHtmlNode[];
};

/** 把有限颜色与标签语义映射为 class / 安全 color style，再清掉任意 style。 */
function rehypeAssistantSafeStyles() {
  return (tree: AssistantHtmlNode) => {
    const visit = (node: AssistantHtmlNode) => {
      if (node.type === "element") {
        const properties = node.properties || (node.properties = {});
        const rawClass = Array.isArray(properties.className)
          ? properties.className.map(String)
          : typeof properties.className === "string"
            ? properties.className.split(/\s+/)
            : [];
        const rawLabel = String(properties.dataLabel || properties["data-label"] || "").toLowerCase();
        const colorAttr = String(properties.color || "").trim();
        const styleColor = extractStyleColor(String(properties.style || ""));
        const fromAttr = resolveAssistantTextColor(colorAttr);
        const fromStyle = styleColor ? resolveAssistantTextColor(styleColor) : {};
        // 属性 color 优先，其次 style 里的 color
        const colorClass = fromAttr.className || fromStyle.className;
        const colorStyle = fromAttr.style || fromStyle.style;
        const requestedLabel = SAFE_LABELS[rawLabel]
          || rawClass.map((item) => SAFE_LABELS[item.replace(/^label-/, "").toLowerCase()]).find(Boolean);
        const safeClasses = [
          colorClass,
          requestedLabel,
          ...rawClass.filter((item) => ASSISTANT_HTML_CLASSES.includes(item)),
        ].filter(Boolean) as string[];
        if (safeClasses.length) properties.className = [...new Set(safeClasses)];
        else delete properties.className;
        if (colorStyle) properties.style = colorStyle;
        else delete properties.style;
        delete properties.color;
        delete properties.dataLabel;
        delete properties["data-label"];
        if (node.tagName === "font") node.tagName = "span";
      }
      node.children?.forEach(visit);
    };
    visit(tree);
  };
}

const ASSISTANT_SANITIZE_SCHEMA = {
  ...defaultSchema,
  attributes: {
    ...defaultSchema.attributes,
    // 允许命名色 class；style 仅保留我们写入的 color: #hex / rgb()
    span: [
      ...(defaultSchema.attributes?.span || []),
      ["className", ...ASSISTANT_HTML_CLASSES],
      "style",
    ],
    font: [
      ...(defaultSchema.attributes?.font || []),
      ["className", ...ASSISTANT_HTML_CLASSES],
      "style",
      "color",
    ],
  },
};
const ASSISTANT_SANITIZE_PLUGIN: [typeof rehypeSanitize, typeof ASSISTANT_SANITIZE_SCHEMA] = [
  rehypeSanitize,
  ASSISTANT_SANITIZE_SCHEMA,
];

/** Markdown + 安全 HTML：先解析原始 HTML，映射有限样式，再消毒。 */
const ASSISTANT_REMARK_PLUGINS = [remarkGfm];
const ASSISTANT_REHYPE_PLUGINS = [rehypeRaw, rehypeAssistantSafeStyles, ASSISTANT_SANITIZE_PLUGIN];

export type LiveBubble = {
  id: string;
  role: "user" | "assistant" | "tool" | "system" | "action";
  text: string;
  reasoning?: string;
  createdAt?: string | null;
  pending?: boolean;
  streaming?: boolean;
  streamFallback?: boolean;
  action?: SystemAgentAction;
  messageId?: number;
  runStatus?: string;
  errorCode?: string | null;
  errorMessage?: string | null;
  retryCount?: number;
  providerSwitch?: SystemAgentProviderSwitch;
  toolApproval?: SystemAgentToolApproval;
  /** 最终回答的 usage 元数据（tokens、run_id 等） */
  usage?: Record<string, unknown> | null;
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

function messageReasoning(msg: SystemAgentMessage): string {
  const value = msg.content?.reasoning;
  return typeof value === "string" ? value : "";
}

async function copyAssistantReply(text: string): Promise<void> {
  try {
    await navigator.clipboard.writeText(text);
    toast.success("回答已复制");
  } catch {
    toast.error("复制失败，请检查浏览器剪贴板权限");
  }
}

function chinaTimestamp(value?: string | null): string | null {
  if (!value) return null;
  const formatted = formatDateTime(value, "Asia/Shanghai");
  return formatted === "-" ? null : `${formatted} · 北京时间`;
}

function approvalToolLabel(tool: SystemAgentToolApproval["tools"][number]): string {
  return systemAgentToolLabel(tool.description);
}

export const AssistantMarkdown = memo(function AssistantMarkdown({
  text,
  streaming = false,
}: {
  text: string;
  streaming?: boolean;
}) {
  const source = streaming ? stabilizeStreamingMarkdown(text) : text;
  return (
    <div className={cn("assistant-md prose-pwa-safe max-w-none text-sm leading-relaxed", streaming && "streaming-md")}>
      <ReactMarkdown
        remarkPlugins={ASSISTANT_REMARK_PLUGINS}
        rehypePlugins={ASSISTANT_REHYPE_PLUGINS}
        components={{
          p: ({ children }) => <p className="my-1 first:mt-0 last:mb-0">{children}</p>,
          ul: ({ children }) => <ul className="my-1 list-disc space-y-0.5 pl-5">{children}</ul>,
          ol: ({ children }) => <ol className="my-1 list-decimal space-y-0.5 pl-5">{children}</ol>,
          h1: ({ children }) => <h1 className="my-2 text-base font-semibold">{children}</h1>,
          h2: ({ children }) => <h2 className="my-2 text-sm font-semibold">{children}</h2>,
          h3: ({ children }) => <h3 className="my-1.5 text-sm font-medium">{children}</h3>,
          blockquote: ({ children }) => (
            <blockquote className="my-2 border-l-2 border-border pl-3 text-muted-foreground">
              {children}
            </blockquote>
          ),
          a: ({ children, ...props }) => (
            <a {...props} target="_blank" rel="noreferrer">
              {children}
            </a>
          ),
          table: ({ children }) => (
            <div className="my-2 max-w-full overflow-x-auto overscroll-x-contain">
              <table className="w-max min-w-full table-auto border-collapse text-left text-xs">
                {children}
              </table>
            </div>
          ),
          th: ({ children }) => (
            <th className="whitespace-nowrap border border-border bg-muted px-2 py-1 font-medium">
              {children}
            </th>
          ),
          td: ({ children }) => (
            <td className="whitespace-nowrap border border-border px-2 py-1 align-top">
              {children}
            </td>
          ),
          pre: ({ children }) => (
            <pre className="my-2 max-w-full overflow-x-auto rounded-md bg-background/80 p-2 text-xs">
              {children}
            </pre>
          ),
          code: ({ className, children, ...props }) => (
            <code className={cn("rounded bg-background/70 px-1 py-0.5 text-xs", className)} {...props}>
              {children}
            </code>
          ),
        }}
      >
        {source}
      </ReactMarkdown>
    </div>
  );
});

export function Conversation({
  messages,
  live,
  onActionUpdated,
  onRetryMessage,
  onEditMessage,
  onRegenerateMessage,
  retryingMessageId,
  busy = false,
  expectedSelection = null,
}: {
  messages: SystemAgentMessage[];
  live?: LiveBubble[];
  onActionUpdated?: (action: SystemAgentAction) => void;
  onRetryMessage?: (
    messageId: number,
    fallbackProviderId?: number,
    approvedTools?: string[],
  ) => void;
  onEditMessage?: (messageId: number, assistantMessageId: number, text: string) => void;
  onRegenerateMessage?: (messageId: number, assistantMessageId: number) => void;
  retryingMessageId?: number | null;
  busy?: boolean;
  /** 本轮希望使用的模型（与实际 meta 对照） */
  expectedSelection?: { providerName?: string; model?: string } | null;
}) {
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const followStreamRef = useRef(true);
  const lastLiveUserIdRef = useRef<string | null>(null);
  const [editingMessageId, setEditingMessageId] = useState<number | null>(null);
  const [editingText, setEditingText] = useState("");
  const persistedItems = visibleConversationMessages(messages).map(
      (m): LiveBubble => ({
        id: `m-${m.id}`,
        role: (m.role === "user" || m.role === "assistant" || m.role === "tool"
          ? m.role
          : "system") as LiveBubble["role"],
        text: messageText(m),
        reasoning: messageReasoning(m),
        createdAt: m.created_at,
        messageId: m.id,
        runStatus: m.run_status,
        errorCode: m.error_code,
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
        streamFallback: Boolean(m.usage?.stream_fallback),
        usage: (m.usage as Record<string, unknown> | null | undefined) ?? null,
      }),
    );
  const items: LiveBubble[] = mergeConversationItems(persistedItems, live || []).filter(
    (item) => item.text || item.pending || item.usage,
  );
  const latestUserMessage = [...messages].reverse().find((message) => message.role === "user");
  const latestAssistantMessage = latestUserMessage
    ? messages.find(
        (message) =>
          message.role === "assistant" && message.id > latestUserMessage.id,
      )
    : undefined;
  const latestPairEditable = Boolean(
    latestUserMessage?.run_status === "succeeded" &&
      latestAssistantMessage?.run_status === "completed",
  );
  const liveTail = (live || []).at(-1);
  const latestLiveUserId = [...(live || [])].reverse().find((item) => item.role === "user")?.id;

  useEffect(() => {
    if (latestLiveUserId && latestLiveUserId !== lastLiveUserIdRef.current) {
      followStreamRef.current = true;
    }
    lastLiveUserIdRef.current = latestLiveUserId || null;
  }, [latestLiveUserId]);

  useEffect(() => {
    const node = scrollRef.current;
    if (!node || !followStreamRef.current) return;
    node.scrollTop = node.scrollHeight;
  }, [items.length, liveTail?.text]);

  useEffect(() => {
    if (
      editingMessageId !== null &&
      (busy ||
        !latestPairEditable ||
        editingMessageId !== latestUserMessage?.id)
    ) {
      setEditingMessageId(null);
    }
  }, [busy, editingMessageId, latestPairEditable, latestUserMessage?.id]);

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
    <div
      ref={scrollRef}
      className="mx-auto flex w-full max-w-3xl flex-1 flex-col gap-3 overflow-y-auto p-4 pb-20 sm:pb-4 xl:max-w-5xl 2xl:max-w-6xl"
      onScroll={(event) => {
        const node = event.currentTarget;
        followStreamRef.current = node.scrollHeight - node.scrollTop - node.clientHeight < 72;
      }}
    >
      {items.map((item) => {
        const isUser = item.role === "user";
        const isTool = item.role === "tool";
        const isAction = item.role === "action" && item.action;
        const isFailedUser = isUser && item.runStatus === "failed" && item.messageId != null;
        const isApprovalRequired = isFailedUser && item.errorCode === "AGENT_TOOL_APPROVAL_REQUIRED";
        const failedRunId =
          isFailedUser && typeof item.usage?.run_id === "string" ? item.usage.run_id : null;
        const switchCandidate = item.providerSwitch?.candidates?.[0];
        const approvalTools = item.toolApproval?.tools || [];
        const isLatestEditableUser =
          latestPairEditable && item.messageId === latestUserMessage?.id;
        const isLatestEditableAssistant =
          latestPairEditable && item.messageId === latestAssistantMessage?.id;
        const isEditing = isLatestEditableUser && editingMessageId === item.messageId;
        const visibleReasoning =
          item.reasoning?.trim() && item.reasoning.trim() !== item.text.trim()
            ? item.reasoning.trim()
            : "";
        const timestamp = chinaTimestamp(item.createdAt);
        return (
          <div
            key={item.id}
            className={cn("flex gap-2", isUser ? "justify-end" : "justify-start")}
          >
            {!isUser ? (
              <div className="mt-1 grid h-7 w-7 shrink-0 self-start place-items-center rounded-full bg-muted">
                {isTool ? <Wrench className="h-3.5 w-3.5" /> : <Bot className="h-3.5 w-3.5" />}
              </div>
            ) : null}
            {isAction && item.action ? (
              <div className="max-w-[85%] min-w-[16rem]">
                <ActionCard action={item.action} onUpdated={onActionUpdated} />
              </div>
            ) : (
              <div className={cn("flex min-w-0 max-w-[85%] flex-col gap-1", isUser && "items-end")}>
                {!isUser && !isTool
                  ? (() => {
                      const usage =
                        item.usage ||
                        (item.messageId != null
                          ? (messages.find((m) => m.id === item.messageId)?.usage as Record<
                              string,
                              unknown
                            > | null)
                          : null);
                      const runId =
                        usage && typeof usage.run_id === "string" ? usage.run_id : null;
                      return runId && !item.streaming && !item.pending ? (
                        <RunTrace runId={runId} defaultOpen={false} className="mb-0.5" />
                      ) : null;
                    })()
                  : null}
                {!isUser && !isTool && visibleReasoning ? (
                  <details className="group w-full max-w-[min(75ch,100%)] rounded-md border border-border/60 bg-muted/20 text-xs text-muted-foreground xl:max-w-[min(96ch,100%)] 2xl:max-w-[min(112ch,100%)]">
                    <summary className="flex min-h-9 cursor-pointer list-none items-center gap-2 px-2.5 py-1.5 font-medium text-foreground/75 marker:hidden">
                      <BrainCircuit className="h-3.5 w-3.5 shrink-0 text-primary/75" />
                      <span>思考过程</span>
                      {item.streaming ? <span className="text-[10px] font-normal text-muted-foreground">生成中</span> : null}
                      <span className="ml-auto text-[10px] font-normal text-muted-foreground group-open:hidden">展开</span>
                      <span className="ml-auto hidden text-[10px] font-normal text-muted-foreground group-open:inline">收起</span>
                    </summary>
                    <div className="max-h-72 overflow-y-auto border-t border-border/50 px-3 py-2 text-foreground/75">
                      <AssistantMarkdown text={visibleReasoning} streaming={Boolean(item.streaming)} />
                    </div>
                  </details>
                ) : null}
                <div
                  className={cn(
                    "max-w-full break-words text-sm leading-relaxed",
                    isUser && !isEditing && "rounded-2xl bg-primary px-3 py-2 text-primary-foreground",
                    isEditing && "w-[min(36rem,78vw)] rounded-lg border border-border bg-card p-2 text-foreground shadow-sm",
                    // 助手回答：无气泡正文（DEEIX / restyle）
                    !isUser && !isTool && "min-w-0 max-w-[min(75ch,100%)] border-l-2 border-primary/35 py-0.5 pl-3 text-foreground xl:max-w-[min(96ch,100%)] 2xl:max-w-[min(112ch,100%)]",
                    isTool && "rounded-2xl border border-dashed bg-background px-3 py-2 text-xs text-muted-foreground",
                    (isUser || isTool) && "whitespace-pre-wrap",
                    item.pending && "opacity-70",
                  )}
                >
                  {isEditing ? (
                    <form
                      className="flex flex-col gap-2"
                      onSubmit={(event) => {
                        event.preventDefault();
                        const nextText = editingText.trim();
                        if (!nextText || item.messageId == null || latestAssistantMessage == null) {
                          return;
                        }
                        onEditMessage?.(item.messageId, latestAssistantMessage.id, nextText);
                        setEditingMessageId(null);
                      }}
                    >
                      <Textarea
                        autoFocus
                        rows={3}
                        maxLength={32_000}
                        value={editingText}
                        className="min-h-24 resize-y text-sm leading-relaxed"
                        aria-label="编辑消息"
                        onChange={(event) => setEditingText(event.target.value)}
                        onKeyDown={(event) => {
                          if (event.key === "Escape") {
                            event.preventDefault();
                            setEditingMessageId(null);
                          } else if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
                            event.preventDefault();
                            event.currentTarget.form?.requestSubmit();
                          }
                        }}
                      />
                      <div className="flex justify-end gap-1">
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon"
                          className="h-8 w-8 text-muted-foreground"
                          title="取消编辑"
                          aria-label="取消编辑"
                          onClick={() => setEditingMessageId(null)}
                        >
                          <X className="h-3.5 w-3.5" />
                        </Button>
                        <Button
                          type="submit"
                          size="icon"
                          className="h-8 w-8"
                          title="保存并重新生成"
                          aria-label="保存并重新生成"
                          disabled={!editingText.trim()}
                        >
                          <Check className="h-3.5 w-3.5" />
                        </Button>
                      </div>
                    </form>
                  ) : !isUser && !isTool ? (
                    item.text.trim() ? (
                      <>
                        {item.streamFallback ? (
                          <span className="mb-2 inline-flex rounded-full border px-2 py-0.5 text-[10px] font-normal text-foreground">
                            完整响应
                          </span>
                        ) : null}
                        {item.streaming ? (
                          <span className="sr-only" role="status" aria-live="polite">
                            正在接收回复
                          </span>
                        ) : null}
                        <AssistantMarkdown text={item.text} streaming={Boolean(item.streaming)} />
                      </>
                    ) : item.streaming || item.pending ? (
                      <StreamingText text="" active waitingLabel="正在等待上游返回首段内容" />
                    ) : null
                  ) : item.text ? (
                    item.text
                  ) : item.pending ? (
                    "思考中…"
                  ) : null}
                </div>
                {!isUser && !isTool && !item.streaming && !item.pending ? (
                  <ModelRunMeta
                    usage={
                      item.usage ||
                      (item.messageId != null
                        ? (messages.find((m) => m.id === item.messageId)?.usage as Record<
                            string,
                            unknown
                          > | null)
                        : null)
                    }
                    expected={expectedSelection}
                  />
                ) : null}
                {!isUser && !isTool && !item.streaming && !item.pending && item.text.trim() ? (
                  <div className="flex min-h-8 w-full max-w-[min(75ch,100%)] items-center gap-1 border-t border-border/45 pr-14 pt-1 text-[10px] text-muted-foreground sm:pr-0 xl:max-w-[min(96ch,100%)] 2xl:max-w-[min(112ch,100%)]">
                    {timestamp ? <time dateTime={item.createdAt || undefined} className="mr-auto tabular-nums">{timestamp}</time> : <span className="mr-auto" />}
                    <Button
                      type="button"
                      variant="ghost"
                      size="icon"
                      className="h-8 w-8 text-muted-foreground"
                      title="复制回答"
                      aria-label="复制回答"
                      onClick={() => void copyAssistantReply(item.text)}
                    >
                      <Copy className="h-3.5 w-3.5" />
                    </Button>
                    {isLatestEditableAssistant && onRegenerateMessage ? (
                      <Button
                        type="button"
                        variant="ghost"
                        size="icon"
                        className="h-8 w-8 text-muted-foreground"
                        title="使用当前选择的模型重新生成"
                        aria-label="使用当前选择的模型重新生成"
                        disabled={busy}
                        onClick={() => {
                          if (latestUserMessage && latestAssistantMessage) {
                            onRegenerateMessage(latestUserMessage.id, latestAssistantMessage.id);
                          }
                        }}
                      >
                        <RotateCcw className="h-3.5 w-3.5" />
                      </Button>
                    ) : null}
                  </div>
                ) : null}
                {isLatestEditableUser && !isEditing && item.text.trim() && onEditMessage ? (
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    className="h-8 w-8 text-muted-foreground"
                    title="编辑并重新生成"
                    aria-label="编辑并重新生成"
                    disabled={busy}
                    onClick={() => {
                      setEditingText(item.text);
                      setEditingMessageId(item.messageId!);
                    }}
                  >
                    <Pencil className="h-3.5 w-3.5" />
                  </Button>
                ) : null}
                {isFailedUser ? (
                  <div className={cn(
                    "flex max-w-full flex-col items-stretch gap-2 rounded-lg border px-2.5 py-2 text-xs sm:flex-row sm:items-start",
                    isApprovalRequired
                      ? "border-warning/35 bg-warning/5 text-warning"
                      : "border-destructive/30 bg-destructive/5 text-destructive",
                  )}>
                    {isApprovalRequired
                      ? <ShieldCheck className="mt-0.5 h-3.5 w-3.5 shrink-0" />
                      : <AlertCircle className="mt-0.5 h-3.5 w-3.5 shrink-0" />}
                    <div className="min-w-0 flex-1">
                      <p className="break-words">
                        {isApprovalRequired
                          ? "已暂停；批准后会重新生成调用，并仅执行已批准的只读工具。"
                          : item.errorMessage || "本轮执行失败"}
                      </p>
                      {approvalTools.length ? (
                        <p className="mt-1 break-words text-foreground/75">
                          准备调用：{approvalTools.map(approvalToolLabel).join("、")}
                        </p>
                      ) : null}
                      {item.retryCount ? <p className="mt-1 opacity-70">已重试 {item.retryCount} 次</p> : null}
                      {failedRunId ? (
                        <RunTrace
                          runId={failedRunId}
                          failed
                          defaultOpen={false}
                          className="mt-1.5 text-foreground"
                        />
                      ) : null}
                    </div>
                    <div className="flex shrink-0 justify-end gap-1.5 self-end sm:self-auto">
                      {approvalTools.length ? (
                        <Button
                          type="button"
                          size="sm"
                          className="h-9 min-w-24 max-w-48 touch-manipulation px-3 text-xs sm:h-7 sm:min-w-0 sm:px-2"
                          title={`批准调用：${approvalTools.map(approvalToolLabel).join("、")}`}
                          disabled={busy || retryingMessageId != null}
                          aria-live="polite"
                          onClick={() => {
                            void onRetryMessage?.(
                              item.messageId!,
                              switchCandidate?.provider_id,
                              approvalTools.map((tool) => tool.name),
                            );
                          }}
                        >
                          {retryingMessageId === item.messageId
                            ? <RotateCcw className="mr-1 h-3 w-3 animate-spin" />
                            : <ShieldCheck className="mr-1 h-3 w-3" />}
                          {retryingMessageId === item.messageId
                            ? "继续中"
                            : switchCandidate
                            ? `批准并改用 ${switchCandidate.provider_name}`
                            : "批准并继续"}
                        </Button>
                      ) : null}
                      {switchCandidate && !approvalTools.length ? (
                        <Button
                          type="button"
                          size="sm"
                          className="h-7 max-w-48 px-2 text-xs"
                          title={`改用 ${switchCandidate.provider_name} · ${switchCandidate.model}`}
                          disabled={busy || retryingMessageId != null}
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
                          disabled={busy || retryingMessageId != null}
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
              <div className="mt-1 grid h-7 w-7 shrink-0 self-start place-items-center rounded-full bg-primary/10">
                <User className="h-3.5 w-3.5" />
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
