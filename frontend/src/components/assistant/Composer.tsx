import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { ListPlus, Menu, Route, Send, Square, StepForward } from "lucide-react";

import {
  ModelPicker,
  type ModelPickerItem,
  type ModelPickerValue,
} from "@/components/ai/ModelPicker";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Select } from "@/components/ui/select";
import { cn } from "@/lib/utils";
import { composerEnterAction } from "./composerState";

export type ComposerAction = "queue" | "steer" | "replace";

export function Composer({
  disabled,
  onSend,
  streaming,
  onStop,
  placeholder,
  modelItems = [],
  modelSelection,
  onModelSelectionChange,
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
  onSend: (text: string) => void | Promise<void>;
  streaming?: boolean;
  onStop?: () => void;
  placeholder?: string;
  /** 全量可选模型（按 Provider 分组） */
  modelItems?: ModelPickerItem[];
  modelSelection?: ModelPickerValue;
  onModelSelectionChange?: (next: ModelPickerValue) => void;
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
  const textareaRef = useRef<HTMLTextAreaElement | null>(null);
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

  const submit = async () => {
    const text = value.trim();
    if (!text || disabled || sending) return;
    setSending(true);
    try {
      await onSend(text);
      updateValue("");
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
      className="shrink-0 border-t bg-background/90 p-2 backdrop-blur sm:p-3"
    >
      <div className="mx-auto max-w-3xl rounded-xl border border-border/80 bg-input-bg/70 p-2 shadow-sm focus-within:border-primary/50 focus-within:ring-2 focus-within:ring-ring/15 xl:max-w-5xl 2xl:max-w-6xl">
        <Textarea
          ref={textareaRef}
          value={value}
          onChange={(e) => updateValue(e.target.value)}
          onKeyDown={onKeyDown}
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
        <div className="mt-1 flex min-w-0 items-end justify-end gap-1.5 border-t border-border/40 pt-2">
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
              会话
            </Button>
          ) : null}
          {expectedLabel ? (
            <span className={cn("hidden max-w-[32%] truncate self-center text-[10px] text-muted-foreground md:inline", onOpenSessions ? "md:mr-auto" : "mr-auto")}>
              本轮：{expectedLabel}
            </span>
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
              className="min-w-0 justify-end"
            />
          ) : null}
          {streaming ? (
            <>
              <label className="min-w-0">
                <span className="sr-only">运行中消息操作</span>
                <Select
                  aria-label="运行中消息操作"
                  value={actionMode}
                  onChange={(event) =>
                    onActionModeChange?.(event.target.value as ComposerAction)
                  }
                  className="h-9 min-w-[7.5rem] py-0 text-xs"
                >
                  <option value="queue">加入队列{queueCount ? ` · ${queueCount}` : ""}</option>
                  <option value="steer" disabled={runStatus !== "running"}>
                    调整当前任务
                  </option>
                  <option
                    value="replace"
                    disabled={
                      !runStatus ||
                      !["running", "waiting_input", "waiting_approval"].includes(runStatus)
                    }
                  >
                    停止并替换
                  </option>
                </Select>
              </label>
              <Button
                type="submit"
                size="sm"
                disabled={disabled || sending || !value.trim()}
                className="h-9 shrink-0 gap-1.5 px-3 text-xs"
                title={
                  actionMode === "steer"
                    ? "将说明注入当前任务"
                    : actionMode === "replace"
                      ? "停止当前任务并立即执行这条消息"
                      : "将消息加入当前会话队列"
                }
              >
                {actionMode === "steer" ? (
                  <Route className="h-4 w-4" />
                ) : actionMode === "replace" ? (
                  <StepForward className="h-4 w-4" />
                ) : (
                  <ListPlus className="h-4 w-4" />
                )}
                {actionMode === "steer"
                  ? "调整"
                  : actionMode === "replace"
                    ? "替换"
                    : "排队"}
              </Button>
              <Button
                type="button"
                variant="outline"
                size="icon"
                className="h-9 w-9 shrink-0"
                onClick={onStop}
                title={`停止当前任务${runStatus ? `（${runStatus}）` : ""}`}
              >
                <Square className="h-4 w-4 fill-current" />
                <span className="sr-only">停止</span>
              </Button>
            </>
          ) : (
            <Button
              type="submit"
              size="icon"
              disabled={disabled || sending || !value.trim()}
              className="h-9 w-9 shrink-0"
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
