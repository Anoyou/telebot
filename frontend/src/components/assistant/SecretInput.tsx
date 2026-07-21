import { useState } from "react";
import { KeyRound } from "lucide-react";
import { toast } from "sonner";

import { api } from "@/lib/api";
import { Button } from "@/components/ui/button";
import { getErrMsg } from "@/lib/api";

export function SecretInput({
  actionId,
  fieldNames,
  onDone,
}: {
  actionId: string;
  fieldNames?: string[];
  onDone?: () => void;
}) {
  const fields = fieldNames?.length ? fieldNames : ["api_key"];
  const [values, setValues] = useState<Record<string, string>>(
    Object.fromEntries(fields.map((f) => [f, ""])),
  );
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    const payload: Record<string, string> = {};
    for (const name of fields) {
      const v = (values[name] || "").trim();
      if (v) payload[name] = v;
    }
    if (Object.keys(payload).length === 0) {
      toast.error("请先填写密钥");
      return;
    }
    setBusy(true);
    try {
      await api.post(`/api/system-agent/actions/${actionId}/secret-input`, {
        fields: payload,
      });
      toast.success("密钥已加密暂存，不会回显");
      setValues(Object.fromEntries(fields.map((f) => [f, ""])));
      onDone?.();
    } catch (e) {
      toast.error(getErrMsg(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="mt-2 space-y-2 rounded-md border border-dashed p-2">
      <div className="flex items-center gap-1 text-xs text-muted-foreground">
        <KeyRound className="h-3.5 w-3.5" />
        可选：补填密钥（仅加密暂存，确认后使用）
      </div>
      {fields.map((name) => (
        <input
          key={name}
          type="password"
          autoComplete="off"
          className="h-8 w-full rounded-md border bg-background px-2 text-sm"
          placeholder={name}
          value={values[name] || ""}
          onChange={(e) => setValues((prev) => ({ ...prev, [name]: e.target.value }))}
        />
      ))}
      <Button type="button" size="sm" variant="outline" loading={busy} onClick={() => void submit()}>
        保存密钥
      </Button>
    </div>
  );
}
