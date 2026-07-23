import { useState, type FormEvent, type KeyboardEvent } from "react";
import { Send, Square } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Select } from "@/components/ui/select";
import { Textarea } from "@/components/ui/textarea";

export function Composer({
  disabled,
  onSend,
  streaming,
  onStop,
  placeholder,
  modelOptions = [],
  modelValue = "",
  onModelChange,
  modelDisabled,
  modelOptionMeta,
  expectedLabel,
}: {
  disabled?: boolean;
  onSend: (text: string) => void | Promise<void>;
  streaming?: boolean;
  onStop?: () => void;
  placeholder?: string;
  modelOptions?: string[];
  modelValue?: string;
  onModelChange?: (model: string) => void;
  modelDisabled?: boolean;
  /** 模型徽标文案，如 Tools / Vision / 实测✓ / 冷却中 */
  modelOptionMeta?: Record<string, string>;
  /** 本轮希望使用说明 */
  expectedLabel?: string;
}) {
  const [value, setValue] = useState("");
  const [sending, setSending] = useState(false);

  const submit = async () => {
    const text = value.trim();
    if (!text || disabled || sending || streaming) return;
    setSending(true);
    try {
      await onSend(text);
      setValue("");
    } finally {
      setSending(false);
    }
  };

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    void submit();
  };

  const onKeyDown = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void submit();
    }
  };

  return (
    <form onSubmit={onSubmit} className="border-t bg-background/80 p-2 backdrop-blur sm:p-3">
      <div className="relative mx-auto max-w-3xl rounded-xl border border-border/80 bg-input-bg/70 p-2 shadow-sm focus-within:border-primary/50 focus-within:ring-2 focus-within:ring-ring/15">
        <Textarea
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={disabled || sending || streaming}
          placeholder={placeholder || "用自然语言查询系统状态…（Enter 发送，Shift+Enter 换行）"}
          rows={2}
          className="min-h-[4.5rem] resize-none border-0 bg-transparent px-1 pb-10 pt-1 shadow-none focus-visible:border-transparent focus-visible:ring-0"
        />
        <div className="absolute inset-x-2 bottom-2 flex items-center justify-end gap-1.5">
          {expectedLabel ? (
            <span className="mr-auto hidden max-w-[40%] truncate text-[10px] text-muted-foreground sm:inline">
              本轮希望使用：{expectedLabel}
            </span>
          ) : null}
          {modelOptions.length > 0 ? (
            <Select
              aria-label="切换 Agent 模型"
              value={modelValue || modelOptions[0] || ""}
              disabled={modelDisabled || streaming}
              onChange={(event) => onModelChange?.(event.target.value)}
              className="h-8 min-w-0 w-[min(16rem,68%)] border-border/60 bg-background/80 px-2 text-xs"
            >
              {modelOptions.map((model) => {
                const meta = modelOptionMeta?.[model];
                return (
                  <option key={model} value={model} disabled={meta?.includes("不可用")}>
                    {meta ? `${model} · ${meta}` : model}
                  </option>
                );
              })}
            </Select>
          ) : null}
          {streaming ? (
            <Button
              type="button"
              variant="outline"
              size="icon"
              className="h-8 w-8 shrink-0"
              onClick={onStop}
              title="停止本轮请求"
            >
              <Square className="h-4 w-4 fill-current" />
              <span className="sr-only">停止</span>
            </Button>
          ) : (
            <Button
              type="submit"
              size="icon"
              disabled={disabled || sending || !value.trim()}
              className="h-8 w-8 shrink-0"
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
