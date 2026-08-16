import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { ImagePlus, ListPlus, Menu, Route, Send, Square, StepForward, X } from "lucide-react";
import { toast } from "sonner";

import {
  ModelPicker,
  type ModelPickerItem,
  type ModelPickerValue,
} from "@/components/ai/ModelPicker";
import { ClientPicker, type ClientPickerValue } from "./ClientPicker";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { cn } from "@/lib/utils";
import { composerEnterAction } from "./composerState";
import type { SystemAgentImageAttachment } from "@/api/systemAgent";

export type ComposerAction = "queue" | "steer" | "replace";
export type ComposerAttachment = SystemAgentImageAttachment;

const ACTION_COPY: Record<
  ComposerAction,
  { label: string; description: string; submitLabel: string }
> = {
  queue: {
    label: "稍后执行",
    description: "等当前任务完成后，再处理这条消息。",
    submitLabel: "加入稍后执行",
  },
  steer: {
    label: "补充要求",
    description: "不中断当前任务，把这条要求补充进去。",
    submitLabel: "补充到当前任务",
  },
  replace: {
    label: "改做这条",
    description: "停止当前任务，立即改做这条消息。",
    submitLabel: "停止并改做",
  },
};

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
  actionMode = "queue",
  onActionModeChange,
  queueCount = 0,
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
  actionMode?: ComposerAction;
  onActionModeChange?: (value: ComposerAction) => void;
  queueCount?: number;
  runStatus?: string | null;
}) {
  const [internalValue, setInternalValue] = useState("");
  const [sending, setSending] = useState(false);
  const [attachments, setAttachments] = useState<ComposerAttachment[]>([]);
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const composingRef = useRef(false);
  const suppressNextEnterRef = useRef(false);
  const suppressTimerRef = useRef<number | null>(null);
  const value = controlledValue ?? internalValue;
  const selectedAction = ACTION_COPY[actionMode];

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
        {streaming ? (
          <div className="mb-2 border-b border-border/50 pb-2">
            <div className="mb-1.5 flex items-center justify-between gap-2 px-0.5">
              <span className="text-xs font-medium">这条消息怎么处理？</span>
              {queueCount > 0 ? (
                <span className="shrink-0 text-[10px] text-muted-foreground">
                  已有 {queueCount} 条稍后执行
                </span>
              ) : null}
            </div>
            <div
              role="radiogroup"
              aria-label="选择这条消息的处理方式"
              className="grid grid-cols-3 gap-1 rounded-lg bg-muted/55 p-1"
            >
              {(Object.keys(ACTION_COPY) as ComposerAction[]).map((mode) => {
                const copy = ACTION_COPY[mode];
                const unavailable =
                  (mode === "steer" && runStatus !== "running") ||
                  (mode === "replace" &&
                    (!runStatus ||
                      !["running", "waiting_input", "waiting_approval"].includes(
                        runStatus,
                      )));
                return (
                  <button
                    key={mode}
                    type="button"
                    role="radio"
                    aria-checked={actionMode === mode}
                    aria-describedby="composer-action-description"
                    disabled={unavailable}
                    onClick={() => onActionModeChange?.(mode)}
                    className={cn(
                      "min-h-9 min-w-0 rounded-md px-1.5 text-xs font-medium transition-colors",
                      "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring/50",
                      "disabled:cursor-not-allowed disabled:opacity-40",
                      actionMode === mode
                        ? "bg-background text-foreground shadow-sm"
                        : "text-muted-foreground hover:bg-background/60 hover:text-foreground",
                    )}
                    title={copy.description}
                  >
                    <span className="block truncate">
                      {copy.label}
                      {mode === "queue" && queueCount > 0 ? ` · ${queueCount}` : ""}
                    </span>
                  </button>
                );
              })}
            </div>
            <p
              id="composer-action-description"
              className="mt-1.5 min-h-4 px-0.5 text-[11px] leading-4 text-muted-foreground"
            >
              {selectedAction.description}
            </p>
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
                size="sm"
                disabled={disabled || sending || (!value.trim() && attachments.length === 0)}
                className="h-9 shrink-0 gap-1.5 px-2.5 text-xs sm:px-3"
                title={selectedAction.description}
              >
                {actionMode === "steer" ? (
                  <Route className="h-4 w-4" />
                ) : actionMode === "replace" ? (
                  <StepForward className="h-4 w-4" />
                ) : (
                  <ListPlus className="h-4 w-4" />
                )}
                <span className="hidden sm:inline">{selectedAction.submitLabel}</span>
                <span className="sm:hidden">{selectedAction.label}</span>
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
