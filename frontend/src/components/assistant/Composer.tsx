import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { Check, ChevronDown, CornerDownRight, ImagePlus, ListPlus, Menu, Route, Send, Square, Trash2, X } from "lucide-react";
import { toast } from "sonner";

import {
  ModelPicker,
  type ModelPickerItem,
  type ModelPickerValue,
} from "@/components/ai/ModelPicker";
import { ClientPicker, type ClientPickerValue } from "./ClientPicker";
import { Button } from "@/components/ui/button";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { composerEnterAction } from "./composerState";
import type { SystemAgentImageAttachment, SystemAgentQueueItem } from "@/api/systemAgent";

export type ComposerAttachment = SystemAgentImageAttachment;

const QUEUE_ACTION_COPY = {
  queue: {
    label: "稍后执行",
    description: "等当前任务完成后，再处理这条消息。",
  },
  steer: {
    label: "补充说明/调整方向",
    description: "把补充信息加入当前任务，Agent 会据此调整后续处理。",
  },
} as const;

export function Composer({
  disabled,
  onSend,
  streaming,
  onStop,
  placeholder,
  modelItems = [],
  modelSelection,
  onModelSelectionChange,
  clientSelection,
  onClientSelectionChange,
  clientDisabled,
  gatewayAvailable,
  onSetDefaultModel,
  modelDisabled,
  expectedLabel,
  onOpenSessions,
  showSessionButtonOnDesktop = false,
  value: controlledValue,
  onValueChange,
  focusRequestKey = 0,
  queueItems = [],
  onDeleteQueueItem,
  onSteerQueueItem,
  runStatus,
}: {
  disabled?: boolean;
  onSend: (text: string, attachments: ComposerAttachment[]) => void | Promise<void>;
  streaming?: boolean;
  onStop?: () => void;
  placeholder?: string;
  /** 全量可选模型（按 Provider 分组） */
  modelItems?: ModelPickerItem[];
  modelSelection?: ModelPickerValue;
  onModelSelectionChange?: (next: ModelPickerValue) => void;
  clientSelection?: ClientPickerValue;
  onClientSelectionChange?: (next: ClientPickerValue) => void;
  clientDisabled?: boolean;
  gatewayAvailable?: boolean;
  onSetDefaultModel?: (providerId: number, model: string) => void;
  modelDisabled?: boolean;
  /** 本轮希望使用说明 */
  expectedLabel?: string;
  onOpenSessions?: () => void;
  showSessionButtonOnDesktop?: boolean;
  value?: string;
  onValueChange?: (value: string) => void;
  focusRequestKey?: number;
  queueItems?: SystemAgentQueueItem[];
  onDeleteQueueItem?: (item: SystemAgentQueueItem) => void | Promise<void>;
  onSteerQueueItem?: (item: SystemAgentQueueItem) => void | Promise<void>;
  runStatus?: string | null;
}) {
  const [internalValue, setInternalValue] = useState("");
  const [sending, setSending] = useState(false);
  const [attachments, setAttachments] = useState<ComposerAttachment[]>([]);
  const [queueMutationId, setQueueMutationId] = useState<string | null>(null);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const composingRef = useRef(false);
  const suppressNextEnterRef = useRef(false);
  const suppressTimerRef = useRef<number | null>(null);
  const value = controlledValue ?? internalValue;

  const updateValue = (next: string) => {
    if (controlledValue === undefined) setInternalValue(next);
    onValueChange?.(next);
  };

  useEffect(() => {
    if (!focusRequestKey) return;
    const node = textareaRef.current;
    if (!node) return;
    node.focus();
    node.setSelectionRange(node.value.length, node.value.length);
  }, [focusRequestKey]);

  useEffect(
    () => () => {
      if (suppressTimerRef.current != null) window.clearTimeout(suppressTimerRef.current);
    },
    [],
  );

  const addFiles = async (files: File[]) => {
    const accepted = files.filter((file) => ["image/jpeg", "image/png", "image/webp", "image/gif"].includes(file.type));
    if (accepted.length !== files.length) toast.error("仅支持 JPG、PNG、WEBP 或 GIF 图片");
    const available = Math.max(0, 4 - attachments.length);
    if (accepted.length > available) toast.error("每条消息最多附带 4 张图片");
    const next = accepted.slice(0, available);
    const loaded = await Promise.all(next.map((file) => new Promise<ComposerAttachment | null>((resolve) => {
      if (file.size > 6 * 1024 * 1024) { toast.error(`${file.name} 超过 6 MiB`); resolve(null); return; }
      const reader = new FileReader();
      reader.onload = () => resolve({ kind: "image", source: "data_url", mime_type: file.type, data_url: String(reader.result || ""), name: file.name });
      reader.onerror = () => { toast.error(`${file.name} 读取失败`); resolve(null); };
      reader.readAsDataURL(file);
    })));
    setAttachments((current) => [
      ...current,
      ...loaded.filter((item): item is ComposerAttachment => Boolean(item)),
    ].slice(0, 4));
  };

  const submit = async () => {
    const text = value.trim();
    if ((!text && attachments.length === 0) || disabled || sending) return;
    setSending(true);
    try {
      await onSend(text, attachments);
      updateValue("");
      setAttachments([]);
    } finally {
      setSending(false);
    }
  };

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    void submit();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    const action = composerEnterAction({
      key: e.key,
      shiftKey: e.shiftKey,
      nativeComposing: e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229,
      compositionActive: composingRef.current,
      suppressAfterComposition: suppressNextEnterRef.current,
    });
    if (action === "suppress") {
      suppressNextEnterRef.current = false;
      e.preventDefault();
      return;
    }
    if (action === "submit") {
      e.preventDefault();
      void submit();
    }
  };

  return (
    <form
      data-assistant-composer
      onSubmit={onSubmit}
      onDragOver={(event) => event.preventDefault()}
      onDrop={(event) => { event.preventDefault(); void addFiles(Array.from(event.dataTransfer.files)); }}
      className="shrink-0 border-t bg-background/90 p-2 backdrop-blur sm:p-3"
    >
      <div className="mx-auto max-w-3xl rounded-xl border border-border/80 bg-input-bg/70 p-2 shadow-sm focus-within:border-primary/50 focus-within:ring-2 focus-within:ring-ring/15 xl:max-w-5xl 2xl:max-w-6xl">
        {streaming && queueItems.length > 0 ? (
          <div
            data-assistant-composer-queue
            className="-mx-2 -mt-2 mb-1 max-h-32 overflow-y-auto rounded-t-xl border-b border-border/70 bg-muted/25"
            aria-label="已提交的待处理消息"
          >
            {queueItems.map((item) => {
              const busy = queueMutationId === item.id;
              return (
                <div
                  key={item.id}
                  className="flex min-h-11 items-center gap-2 border-b border-border/45 px-2.5 py-1.5 last:border-b-0 sm:px-3"
                >
                  <CornerDownRight className="h-4 w-4 shrink-0 text-muted-foreground" />
                  <span
                    className="min-w-0 flex-1 truncate text-xs font-medium sm:text-sm"
                    title={item.content || "图片消息"}
                  >
                    {item.content || "图片消息"}
                  </span>
                  <DropdownMenu>
                    <DropdownMenuTrigger asChild>
                      <Button
                        type="button"
                        variant="ghost"
                        size="sm"
                        disabled={busy}
                        className="h-8 shrink-0 gap-1 px-2 text-[11px] text-muted-foreground sm:text-xs"
                        aria-label={`选择“${item.content || "图片消息"}”的用途`}
                      >
                        <ListPlus className="h-3.5 w-3.5" />
                        <span className="hidden xs:inline">稍后执行</span>
                        <ChevronDown className="h-3 w-3" />
                      </Button>
                    </DropdownMenuTrigger>
                    <DropdownMenuContent
                      align="end"
                      className="w-[min(20rem,calc(100vw-1.5rem))] p-1"
                    >
                      <DropdownMenuItem className="items-start gap-2 py-2.5">
                        <ListPlus className="mt-0.5 h-4 w-4 shrink-0" />
                        <span className="min-w-0 flex-1">
                          <span className="block text-sm font-medium">
                            {QUEUE_ACTION_COPY.queue.label}
                          </span>
                          <span className="mt-0.5 block text-[11px] leading-4 text-muted-foreground">
                            {QUEUE_ACTION_COPY.queue.description}
                          </span>
                        </span>
                        <Check className="mt-0.5 h-3.5 w-3.5 shrink-0 text-primary" />
                      </DropdownMenuItem>
                      <DropdownMenuItem
                        disabled={runStatus !== "running" || busy}
                        className="items-start gap-2 py-2.5"
                        onSelect={() => {
                          setQueueMutationId(item.id);
                          void Promise.resolve(onSteerQueueItem?.(item)).finally(() => {
                            setQueueMutationId((current) => current === item.id ? null : current);
                          });
                        }}
                      >
                        <Route className="mt-0.5 h-4 w-4 shrink-0" />
                        <span className="min-w-0 flex-1">
                          <span className="block text-sm font-medium">
                            {QUEUE_ACTION_COPY.steer.label}
                          </span>
                          <span className="mt-0.5 block text-[11px] leading-4 text-muted-foreground">
                            {runStatus === "running"
                              ? QUEUE_ACTION_COPY.steer.description
                              : "当前任务恢复运行后才可调整方向。"}
                          </span>
                        </span>
                      </DropdownMenuItem>
                    </DropdownMenuContent>
                  </DropdownMenu>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    disabled={busy}
                    className="h-8 w-8 shrink-0 text-muted-foreground hover:text-destructive"
                    title="删除这条待处理消息"
                    aria-label={`删除“${item.content || "图片消息"}”`}
                    onClick={() => {
                      setQueueMutationId(item.id);
                      void Promise.resolve(onDeleteQueueItem?.(item)).finally(() => {
                        setQueueMutationId((current) => current === item.id ? null : current);
                      });
                    }}
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </Button>
                </div>
              );
            })}
          </div>
        ) : null}
        {attachments.length > 0 ? (
          <div className="mb-2 flex flex-wrap gap-2" aria-label="待发送图片">
            {attachments.map((item, index) => (
              <div key={`${item.name || "image"}-${index}`} className="group relative h-16 w-16 overflow-hidden rounded-md border border-border bg-muted">
                <img src={item.data_url || item.url || ""} alt={item.name || `图片 ${index + 1}`} className="h-full w-full object-cover" />
                <button type="button" className="absolute right-0.5 top-0.5 rounded-full bg-background/85 p-0.5 opacity-0 transition-opacity group-hover:opacity-100" onClick={() => setAttachments((current) => current.filter((_, i) => i !== index))} aria-label="移除图片">
                  <X className="h-3 w-3" />
                </button>
              </div>
            ))}
          </div>
        ) : null}
        <Textarea
          ref={textareaRef}
          value={value}
          onChange={(e) => updateValue(e.target.value)}
          onKeyDown={onKeyDown}
          onPaste={(event) => {
            const files = Array.from(event.clipboardData.items).map((item) => item.kind === "file" ? item.getAsFile() : null).filter((file): file is File => Boolean(file));
            if (files.length) { event.preventDefault(); void addFiles(files); }
          }}
          onCompositionStart={() => {
            composingRef.current = true;
            suppressNextEnterRef.current = false;
          }}
          onCompositionEnd={() => {
            composingRef.current = false;
            suppressNextEnterRef.current = true;
            if (suppressTimerRef.current != null) window.clearTimeout(suppressTimerRef.current);
            suppressTimerRef.current = window.setTimeout(() => {
              suppressNextEnterRef.current = false;
              suppressTimerRef.current = null;
            }, 0);
          }}
          disabled={disabled || sending}
          placeholder={placeholder || "想让 Agent 怎么帮你？直接用自然语言问她吧！"}
          rows={2}
          className="min-h-[4.5rem] resize-none border-0 bg-transparent px-1 py-1 shadow-none focus-visible:border-transparent focus-visible:ring-0"
        />
        <div className="mt-1 flex min-w-0 flex-nowrap items-end justify-end gap-1 border-t border-border/40 pt-2 sm:gap-1.5">
          <input ref={fileInputRef} type="file" accept="image/jpeg,image/png,image/webp,image/gif" multiple className="hidden" onChange={(event) => { void addFiles(Array.from(event.target.files || [])); event.currentTarget.value = ""; }} />
          <Button type="button" variant="ghost" size="icon" className="mr-1 h-8 w-8 shrink-0" onClick={() => fileInputRef.current?.click()} disabled={disabled || sending} title="添加图片">
            <ImagePlus className="h-4 w-4" /><span className="sr-only">添加图片</span>
          </Button>
          {onOpenSessions ? (
            <Button
              type="button"
              variant="outline"
              size="sm"
              className={cn(
                "mr-auto h-8 shrink-0 gap-1 px-2 text-xs",
                showSessionButtonOnDesktop ? "inline-flex" : "md:hidden",
              )}
              onClick={onOpenSessions}
              aria-label="打开会话列表"
            >
              <Menu className="h-3.5 w-3.5" />
              <span className="hidden sm:inline">会话</span>
            </Button>
          ) : null}
          {expectedLabel ? (
            <span className={cn("hidden max-w-[32%] truncate self-center text-[10px] text-muted-foreground md:inline", onOpenSessions ? "md:mr-auto" : "mr-auto")}>
              本轮：{expectedLabel}
            </span>
          ) : null}
          {clientSelection && onClientSelectionChange ? (
            <ClientPicker
              value={clientSelection}
              onChange={onClientSelectionChange}
              disabled={clientDisabled}
              gatewayAvailable={gatewayAvailable}
              className="shrink-0"
            />
          ) : null}
          {modelItems.length > 0 && modelSelection && onModelSelectionChange ? (
            <ModelPicker
              items={modelItems}
              value={modelSelection}
              onChange={onModelSelectionChange}
              onSetDefault={onSetDefaultModel}
              showSetDefault={Boolean(onSetDefaultModel)}
              disabled={modelDisabled}
              compact
              className="min-w-0 flex-1 justify-end"
            />
          ) : null}
          {streaming ? (
            <>
              <Button
                type="submit"
                size="icon"
                disabled={disabled || sending || (!value.trim() && attachments.length === 0)}
                className="h-9 w-9 shrink-0 rounded-full"
                title="发送并加入稍后执行"
              >
                <Send className="h-4 w-4" />
                <span className="sr-only">发送并加入稍后执行</span>
              </Button>
              <Button
                type="button"
                variant="outline"
                size="icon"
                className="h-9 w-9 shrink-0 rounded-full"
                onClick={onStop}
                title="停止当前任务"
              >
                <Square className="h-4 w-4 fill-current" />
                <span className="sr-only">停止</span>
              </Button>
            </>
          ) : (
            <Button
              type="submit"
              size="icon"
              disabled={disabled || sending || (!value.trim() && attachments.length === 0)}
              className="h-9 w-9 shrink-0 rounded-full"
              title="发送消息"
            >
              <Send className="h-4 w-4" />
              <span className="sr-only">发送</span>
            </Button>
          )}
        </div>
      </div>
    </form>
  );
}
