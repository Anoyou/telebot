import { useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";

import { RunTrace } from "@/components/assistant/RunTrace";
import { cn } from "@/lib/utils";

export type ResponseMetaUsage = Record<string, unknown> | null | undefined;

function num(value: unknown): number | null {
  if (typeof value === "number" && Number.isFinite(value)) return value;
  if (typeof value === "string" && value.trim() && Number.isFinite(Number(value))) {
    return Number(value);
  }
  return null;
}

function str(value: unknown): string | null {
  if (typeof value === "string" && value.trim()) return value.trim();
  return null;
}

export function ResponseMeta({
  usage,
  expected,
  className,
}: {
  usage?: ResponseMetaUsage;
  /** 本轮希望使用的 Provider·模型（与实际不一致时高亮） */
  expected?: { providerName?: string; model?: string } | null;
  className?: string;
}) {
  const [open, setOpen] = useState(false);
  if (!usage || typeof usage !== "object") return null;

  const provider = str(usage.provider_name);
  const model = str(usage.model);
  const input = num(usage.input_tokens);
  const output = num(usage.output_tokens);
  const tools = num(usage.tool_count);
  const retries = num(usage.retry_count);
  const elapsed = num(usage.elapsed_ms);
  const runId = str(usage.run_id);
  const usedFallback = Boolean(usage.used_fallback);
  const streamFallback = Boolean(usage.stream_fallback);
  const actual = [provider, model].filter(Boolean).join(" · ");
  const expectedLabel = [expected?.providerName, expected?.model].filter(Boolean).join(" · ");
  const mismatch =
    Boolean(expectedLabel) &&
    Boolean(actual) &&
    expectedLabel !== actual;

  const bits: string[] = [];
  if (actual) bits.push(actual);
  if (input != null || output != null) {
    bits.push(`in ${input ?? "–"} / out ${output ?? "–"}`);
  }
  if (tools != null) bits.push(`工具 ${tools}`);
  if (retries != null && retries > 0) bits.push(`重试 ${retries}`);
  if (elapsed != null) {
    bits.push(elapsed >= 1000 ? `${(elapsed / 1000).toFixed(1)}s` : `${elapsed}ms`);
  }

  if (!bits.length && !runId) return null;

  return (
    <div className={cn("mt-1 space-y-1 text-[11px] leading-4 text-muted-foreground", className)}>
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
        {bits.map((bit) => (
          <span key={bit} className={cn(mismatch && bit === actual && "font-medium text-warning")}>
            {bit}
          </span>
        ))}
        {usedFallback ? (
          <span className="rounded border border-warning/40 px-1 text-warning">fallback</span>
        ) : null}
        {streamFallback ? (
          <span className="rounded border px-1">完整响应</span>
        ) : null}
        {runId ? (
          <button
            type="button"
            className="inline-flex items-center gap-0.5 text-primary hover:underline"
            onClick={() => setOpen((v) => !v)}
            aria-expanded={open}
          >
            {open ? <ChevronDown className="h-3 w-3" /> : <ChevronRight className="h-3 w-3" />}
            执行轨迹
          </button>
        ) : null}
      </div>
      {mismatch ? (
        <div className="text-[10px] text-muted-foreground">
          本轮希望使用 {expectedLabel}；实际使用 {actual}
        </div>
      ) : null}
      {open && runId ? <RunTrace runId={runId} /> : null}
    </div>
  );
}
