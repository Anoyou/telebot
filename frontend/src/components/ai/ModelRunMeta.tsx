import { cn } from "@/lib/utils";

export type ModelRunMetaUsage = Record<string, unknown> | null | undefined;

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

/** 工具调用数：优先实际调用 tool_calls，兼容旧 tool_count（历史消息）。 */
export function resolveToolCallCount(usage: ModelRunMetaUsage): number | null {
  if (!usage || typeof usage !== "object") return null;
  const actual = num(usage.tool_calls);
  if (actual != null) return actual;
  // schema_version>=2 前 tool_count 是暴露数，无法区分；仅当无 tool_calls 时降级
  if (num(usage.schema_version) === 2) return actual;
  return num(usage.tool_count);
}

function formatElapsed(ms: number | null): string | null {
  if (ms == null) return null;
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${ms}ms`;
}

/**
 * 模型执行信息行（Agent 回答下方 / 测活结果复用）。
 * 不显示费用。
 */
export function ModelRunMeta({
  usage,
  expected,
  className,
  compact,
}: {
  usage?: ModelRunMetaUsage;
  expected?: { providerName?: string; model?: string } | null;
  className?: string;
  /** 单行紧凑模式（测活列表） */
  compact?: boolean;
}) {
  if (!usage || typeof usage !== "object") return null;

  const provider = str(usage.provider_name);
  const model = str(usage.model);
  const input = num(usage.input_tokens);
  const output = num(usage.output_tokens);
  const tools = resolveToolCallCount(usage);
  const retries = num(usage.retry_count);
  const elapsed = num(usage.elapsed_ms);
  const apiFormat = str(usage.api_format);
  const usedFallback = Boolean(usage.used_fallback);
  const streamFallback = Boolean(usage.stream_fallback);
  const selectionMode = str(usage.selection_mode);
  const reqProvider = str(usage.requested_provider_name);
  const reqModel = str(usage.requested_model);
  const reported = [provider, model].filter(Boolean).join(" · ");
  const requested = [reqProvider, reqModel].filter(Boolean).join(" · ");
  const summaryModel =
    !usedFallback && reqModel && (!reqProvider || reqProvider === provider) ? reqModel : model;
  const summary = [provider || reqProvider, summaryModel].filter(Boolean).join(" · ");
  const expectedLabel = [expected?.providerName, expected?.model].filter(Boolean).join(" · ");
  const mismatch =
    Boolean(expectedLabel) && Boolean(reported) && expectedLabel !== reported;

  const bits: string[] = [];
  if (summary) bits.push(summary);
  if (input != null || output != null) {
    bits.push(`in ${input ?? "–"} / out ${output ?? "–"}`);
  }
  if (tools != null) bits.push(`工具 ${tools}`);
  if (retries != null && retries > 0) bits.push(`重试 ${retries}`);
  const elapsedLabel = formatElapsed(elapsed);
  if (elapsedLabel) bits.push(elapsedLabel);
  if (apiFormat && !compact) bits.push(apiFormat);

  if (!bits.length) return null;

  return (
    <div
      className={cn(
        "space-y-0.5 text-[11px] leading-4 text-muted-foreground tabular-nums",
        className,
      )}
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-0.5">
        {bits.map((bit) => (
          <span
            key={bit}
            className={cn(usedFallback && mismatch && bit === summary && "font-medium text-warning")}
          >
            {bit}
          </span>
        ))}
        {usedFallback ? (
          <span className="rounded border border-warning/40 px-1 text-warning">fallback</span>
        ) : null}
        {streamFallback ? <span className="rounded border px-1">完整响应</span> : null}
        {selectionMode === "pinned" ? (
          <span className="rounded border px-1">本轮固定</span>
        ) : null}
      </div>
      {usedFallback && requested && reported && requested !== reported ? (
        <div className="space-y-0.5 text-[10px] text-muted-foreground">
          <div>原模型：{requested}</div>
          <div>实际使用：{reported}</div>
          <div>原因：主模型不可用或已切换</div>
        </div>
      ) : null}
      {mismatch && !usedFallback ? (
        <div className="text-[10px] text-muted-foreground">
          本轮请求 {expectedLabel}；上游返回模型标识 {reported}
        </div>
      ) : null}
    </div>
  );
}
