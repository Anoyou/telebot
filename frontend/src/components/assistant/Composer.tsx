import { useState, type FormEvent, type KeyboardEvent } from "react";
import { Send, Square } from "lucide-react";

import {
  ModelPicker,
  type ModelPickerItem,
  type ModelPickerValue,
} from "@/components/ai/ModelPicker";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

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
      <div className="relative mx-auto max-w-3xl rounded-xl border border-border/80 bg-input-bg/70 p-2 shadow-sm focus-within:border-primary/50 focus-within:ring-2 focus-within:ring-ring/15 xl:max-w-5xl 2xl:max-w-6xl">
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
            <span className="mr-auto hidden max-w-[32%] truncate text-[10px] text-muted-foreground sm:inline">
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
              disabled={modelDisabled || streaming}
            />
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
