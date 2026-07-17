import { useState, type FormEvent, type KeyboardEvent } from "react";
import { Send } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

export function Composer({
  disabled,
  onSend,
  placeholder,
}: {
  disabled?: boolean;
  onSend: (text: string) => void | Promise<void>;
  placeholder?: string;
}) {
  const [value, setValue] = useState("");
  const [sending, setSending] = useState(false);

  const submit = async () => {
    const text = value.trim();
    if (!text || disabled || sending) return;
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
    <form onSubmit={onSubmit} className="border-t bg-background/80 p-3 backdrop-blur">
      <div className="mx-auto flex max-w-3xl items-end gap-2">
        <Textarea
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={onKeyDown}
          disabled={disabled || sending}
          placeholder={placeholder || "用自然语言查询系统状态…（Enter 发送，Shift+Enter 换行）"}
          rows={2}
          className="min-h-[2.75rem] resize-none"
        />
        <Button type="submit" disabled={disabled || sending || !value.trim()} className="shrink-0">
          <Send className="h-4 w-4" />
          <span className="sr-only">发送</span>
        </Button>
      </div>
    </form>
  );
}
